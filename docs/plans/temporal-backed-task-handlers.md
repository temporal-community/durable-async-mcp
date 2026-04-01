# Plan: Replace Docket/Redis Task Layer with Temporal-Backed MCP Task Handlers

## Context

The MCP server uses FastMCP's `task=True` decorator, which routes task execution through Docket/Redis for background job management. This is redundant because Temporal already provides durable execution, status queries, signals, and cancellation. The Docket layer also can't represent the `input_required` MCP task state, making the approval elicitation flow broken at the protocol level. We're removing this middleman so MCP tasks map directly to Temporal workflows.

**Key insight:** Use the Temporal workflow ID as the MCP task ID (1:1 mapping, no lookup table needed).

## Files to Change

| File | Action | Purpose |
|---|---|---|
| `temporal_task_handlers.py` | **Create** | Temporal-backed MCP task protocol handlers |
| `server.py` | **Modify** | Simplify `process_invoice`, register custom handlers |
| `workflows.py` | **Modify** | Add `GetInvoiceData` query for elicitation context |
| `tests/test_task_handlers.py` | **Create** | Tests for handler logic and status mapping |
| `tests/__init__.py` | **Create** | Test package init |
| `CLAUDE.md` | **Update** | Reflect new architecture |
| `docs/design/mcp-tasks-architecture.md` | **Update** | Reflect new architecture |

## Approach

### 1. Override FastMCP's request handlers after init

FastMCP registers Docket-based task handlers in `__init__()` via `_setup_task_protocol_handlers()` (`fastmcp/server/server.py:858-908`). These are stored in `mcp._mcp_server.request_handlers[RequestType]`. After FastMCP init completes, we overwrite all 5 relevant handlers with our Temporal-backed versions:

```python
# In server.py, after mcp = FastMCP(...)
from temporal_task_handlers import register_temporal_task_handlers
register_temporal_task_handlers(mcp)
```

The `register_temporal_task_handlers()` function:
1. Saves the original `CallToolRequest` handler
2. Overwrites handlers for: `CallToolRequest` (wrapped), `GetTaskRequest`, `GetTaskPayloadRequest`, `ListTasksRequest`, `CancelTaskRequest`

### 2. Intercept task-augmented `tools/call` for `process_invoice`

Wrap the existing `CallToolRequest` handler. When the request is for `process_invoice` AND has task metadata (checked via `mcp._mcp_server.request_context.experimental.is_task`):
- Start the Temporal workflow
- Return `ServerResult(CallToolResult(...))` with task metadata containing `taskId` (= workflow_id) and `status: "working"`

All other tool calls delegate to FastMCP's original handler unchanged.

### 3. Keep `task=True` on `process_invoice` decorator

This ensures `tools/list` advertises `execution.taskSupport: "optional"` for the tool. FastMCP sets up Docket with in-memory backend by default, but our interceptor prevents it from ever being used for `process_invoice`.

### 4. Simplify `process_invoice` function

The tool function becomes minimal — starts the Temporal workflow and returns a dict. No more inline polling, elicitation, or waiting. When called synchronously (no task metadata), the client gets back the workflow ID and can use `invoice_status` to check on it.

```python
@mcp.tool(task=True)
async def process_invoice(invoice: Dict) -> Dict:
    client = await _client()
    workflow_id = f"invoice-{uuid.uuid4()}"
    await client.start_workflow(InvoiceWorkflow.run, invoice,
        id=workflow_id, task_queue="invoice-task-queue")
    return {"workflow_id": workflow_id, "invoice_id": invoice.get("invoice_id")}
```

### 5. Temporal status → MCP task state mapping

```
INITIALIZING       → working
PENDING-VALIDATION → working
PENDING-APPROVAL   → input_required
APPROVED           → working
PAYING             → working
PAID               → completed
FAILED             → failed
REJECTED           → completed
```

### 6. New file: `temporal_task_handlers.py`

**`register_temporal_task_handlers(mcp)`** — Entry point, overwrites all 5 handlers.

**`handle_tasks_get`** — Queries `GetInvoiceStatus` on the Temporal workflow, maps to MCP task state, returns `GetTaskResult` with `taskId`, `status`, `createdAt` (from `handle.describe().start_time`), `lastUpdatedAt`, `ttl` (5 days), `pollInterval` (2s), `statusMessage`.

**`handle_tasks_result`** — The most complex handler:
- If workflow is in terminal state (`PAID`/`FAILED`/`REJECTED`): return `CallToolResult` with the result data + `io.modelcontextprotocol/related-task` metadata
- If `PENDING-APPROVAL`: create a `fastmcp.server.context.Context`, call `ctx.elicit()` to request approval, signal workflow with response, then block on `handle.result()` until workflow completes, return final result
- If still `working`: raise `McpError(INVALID_PARAMS, "Task not completed yet")`

**`handle_tasks_list`** — Uses `client.list_workflows('WorkflowType = "InvoiceWorkflow"')`, queries each running workflow for granular status, returns `ListTasksResult`.

**`handle_tasks_cancel`** — Calls `handle.cancel()` on the Temporal workflow. Checks for already-terminal workflows first (spec requires `-32602` error for cancelling terminal tasks).

**`make_wrapped_call_tool(original_handler, server)`** — Returns a closure that intercepts task-augmented `process_invoice` calls, starts the workflow, returns task metadata. Delegates everything else to FastMCP's original handler.

### 7. Add `GetInvoiceData` query to `InvoiceWorkflow`

The `tasks/result` handler needs invoice details to build the elicitation message (customer name, amount, line count). Add `self.invoice = invoice` in the `run()` method and a `GetInvoiceData` query that returns it. This keeps the handler self-contained — no external storage needed.

### 8. Error handling

| Scenario | Response |
|---|---|
| Workflow not found | `McpError(INVALID_PARAMS, "Task {id} not found")` |
| Cancel already-terminal task | `McpError(INVALID_PARAMS, "Cannot cancel task: already in terminal status")` |
| `tasks/result` on non-terminal task | `McpError(INVALID_PARAMS, "Task not completed yet")` |
| Temporal unavailable | Let `RPCError` propagate → FastMCP returns `INTERNAL_ERROR` |
| Elicitation declined/cancelled | Signal `RejectInvoice`, await result, return terminal state |

### 9. Temporal client caching

Use a module-level cached client (initialized on first call) to avoid creating a new connection per handler invocation. Reuse the existing `_client()` pattern from `server.py`.

## Task Lifecycle (New Architecture)

```
Client                          MCP Server                    Temporal
  |                               |                            |
  |-- tools/call (task meta) ---->|                            |
  |  (process_invoice)            |-- start_workflow --------->|
  |<-- CallToolResult ------------|  (taskId = workflow_id)    |
  |   (taskId, status:working)    |                            |
  |                               |                            |
  |-- tasks/get(taskId) --------->|-- query GetInvoiceStatus ->|
  |<-- status:working ------------|<-- PENDING-VALIDATION -----|
  |                               |                            |
  |-- tasks/get(taskId) --------->|-- query GetInvoiceStatus ->|
  |<-- status:input_required -----|<-- PENDING-APPROVAL -------|
  |                               |                            |
  |-- tasks/result(taskId) ------>|                            |
  |<-- elicitation: approve? -----|  (ctx.elicit within        |
  |-- user responds: approve ---->|   tasks/result handler)    |
  |                               |-- signal ApproveInvoice -->|
  |                               |-- handle.result() -------->|
  |                               |<-- "PAID" -----------------|
  |<-- CallToolResult ------------|                            |
  |   (status: PAID)              |                            |
```

## Verification

1. **Unit tests**: Status mapping coverage, error cases
2. **Integration tests** (using `temporalio.testing.WorkflowEnvironment`):
   - `tasks/get` returns correct states at each workflow stage
   - `tasks/cancel` cancels workflow
   - `tasks/list` returns active workflows
   - `tasks/result` returns final result for completed workflows
   - Task-augmented tool call starts workflow and returns task metadata
3. **Manual end-to-end**: Start Temporal dev server + worker, run MCP server, call `process_invoice` with task metadata, poll `tasks/get`, call `tasks/result` to approve, verify final result
