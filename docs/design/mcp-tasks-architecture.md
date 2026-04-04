# MCP Tasks Architecture — Invoice Processing

## Overview

This document describes the architecture of the `mcp-tasks` branch, which uses MCP Tasks (SEP-1686) and MCP Elicitation for async invoice processing. Custom Temporal-backed task handlers replace FastMCP's default Docket/Redis layer, mapping the MCP task lifecycle directly to Temporal workflow operations.

**Key insight:** The Temporal workflow ID is used as the MCP task ID (1:1 mapping). No lookup table, Redis, or intermediate storage is needed.

## Tool Surface

| Tool | Task-enabled | Purpose |
|------|-------------|---------|
| `process_invoice` | Yes (`task=TaskConfig(mode="required")`) | Starts Temporal workflow, returns task ID immediately |
| `invoice_status` | No | Query Temporal workflow when task ID is unavailable |

## Architecture

### Handler Registration

FastMCP registers Docket-based task handlers in `_setup_task_protocol_handlers()`. After FastMCP init, `register_temporal_task_handlers(mcp)` overwrites all 5 handlers:

1. **`CallToolRequest`** — Wrapped to intercept task-augmented `process_invoice` calls (starts workflow, returns task stub). All other tools delegate to FastMCP's original handler.
2. **`GetTaskRequest` (tasks/get)** — Queries `GetInvoiceStatus` on the Temporal workflow, maps to MCP task state.
3. **`GetTaskPayloadRequest` (tasks/result)** — Returns final result for terminal workflows. For `PENDING-APPROVAL`: triggers elicitation, signals workflow, awaits completion.
4. **`ListTasksRequest` (tasks/list)** — Lists active invoice workflows via Temporal's `list_workflows` API.
5. **`CancelTaskRequest` (tasks/cancel)** — Cancels running workflows via Temporal's cancel API.

### Why Not Docket/Redis?

Temporal already provides everything Docket does (and more):
- Durable execution and status tracking
- Query handlers for granular status
- Signal handlers for approval/rejection
- Cancellation support
- Automatic retry and timeout handling

The Docket layer couldn't represent the `input_required` MCP task state, making the approval elicitation flow broken at the protocol level.

## Task Lifecycle

```
Client                          MCP Server                    Temporal
  |                               |                            |
  |-- tools/call (task meta) ---->|                            |
  |  (process_invoice)            |-- start_workflow --------->|
  |<-- CreateTaskResult ----------|  (taskId = workflow_id)    |
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
  |                               |                            |
  |  (client cancels request,     |-- signal ApproveInvoice -->|
  |   resumes polling per spec)   |-- handle.result() -------->|
  |                               |   (blocks until terminal,  |
  |-- tasks/get(taskId) --------->|    response discarded)     |
  |<-- status:working ------------|                            |
  |                               |                            |
  |-- tasks/get(taskId) --------->|                            |
  |<-- status:completed ----------|<-- "PAID" -----------------|
  |                               |                            |
  |-- tasks/result(taskId) ------>|                            |
  |<-- CallToolResult ------------|                            |
  |   (status: PAID)              |                            |
```

## State Mapping

| Temporal Workflow Status | MCP Task State    | Notes |
|-------------------------|-------------------|-------|
| INITIALIZING            | working           | Workflow just started |
| PENDING-VALIDATION      | working           | Validation activity running |
| PENDING-APPROVAL        | input_required    | Waiting for human approval |
| APPROVED                | working           | Approval signal received |
| PAYING                  | working           | Child workflows processing line items |
| PAID                    | completed         | All line items paid |
| FAILED                  | failed            | Validation or payment failure |
| REJECTED                | completed         | User rejected the invoice |

## Elicitation Flow

Elicitation happens inside the `tasks/result` handler when the workflow is in `PENDING-APPROVAL` state. The handler:

1. Queries `GetInvoiceData` to get invoice context (customer name, amount, line count)
2. Calls `ctx.elicit()` with approve/reject options
3. Signals the workflow based on the response
4. Blocks on `await handle.result()` until the workflow reaches a terminal state
5. Returns a `CallToolResult` with the terminal status

```python
response = await ctx.elicit(
    message=f"Invoice {invoice_id} for {customer} (${total_amount:.2f}) ...",
    response_type=["approve", "reject"],
)
```

Both `decline` and `cancel` actions map to a workflow rejection signal.

### Client Cancellation After Elicitation

Per the MCP Tasks spec sequence diagram ("Client closes result stream and resumes polling"), the client does **not** wait for the `tasks/result` response after elicitation completes. Instead:

1. Client sends `tasks/result` as a background asyncio task (triggers elicitation on the server)
2. User provides input (approve/reject), elicitation handler returns `ElicitResult`
3. Client cancels the background task (`result_task.cancel()`) and resumes polling `tasks/get`
4. Client polls until the task reaches a terminal state, then calls `tasks/result` again for the final result

The client-side `cancel()` only cancels the Python coroutine — it does **not** send a cancellation message to the server over the MCP protocol. The server-side handler continues running: it signals the workflow, blocks on `await handle.result()`, and eventually returns a `CallToolResult`. The MCP server sends that JSON-RPC response over stdio, but the client has moved on and discards it (the response ID matches a request it no longer tracks).

This is spec-conformant:
- **Server**: `tasks/result` blocks until terminal (Result Retrieval #3: "MUST block the response until the task reaches a terminal status")
- **Client**: closes the result stream after elicitation (per the spec's sequence diagram) and retrieves the final result with a second `tasks/result` call after polling finds the terminal state

### Production Considerations

The abandoned server-side coroutine is benign for a demo but has implications at scale:
- Server resources (the coroutine, the Temporal handle) are held until the workflow completes — could be seconds or days depending on payment due dates
- Multiple abandoned coroutines can accumulate if the client restarts repeatedly
- A production implementation should decouple the `tasks/result` handler from the workflow wait — e.g., using a task store (Redis, database, or a Temporal workflow) that a background process updates when workflows complete

## Error Handling

| Scenario | Response |
|---|---|
| Workflow not found | `McpError(INVALID_PARAMS, "Task {id} not found")` |
| Cancel already-terminal task | `McpError(INVALID_PARAMS, "Cannot cancel task: already in terminal status")` |
| `tasks/result` on non-terminal task | `McpError(INVALID_PARAMS, "Task not completed yet")` |
| Temporal unavailable | `RPCError` propagates → FastMCP returns `INTERNAL_ERROR` |
| Elicitation declined/cancelled | Signal `RejectInvoice`, await result, return terminal state |

## Workflow Queries

The `InvoiceWorkflow` exposes three queries:
- **`GetInvoiceStatus`** — Returns current status string (e.g. `"PENDING-APPROVAL"`)
- **`GetInvoiceData`** — Returns the original invoice dict (used for elicitation context)
- **`IsInvoiceApproved`** — Returns approval boolean (raises if not yet decided)

## Key Dependencies

- **fastmcp >= 2.14.0** — required for `task=True` and `ctx.elicit()`
- **temporalio >= 1.0.0** — Temporal Python SDK
- Temporal server (local dev via `temporal server start-dev`)

## Testing

Tests use `temporalio.testing.WorkflowEnvironment.start_time_skipping()` — an embedded in-process Temporal test server with time-skipping support. No external Temporal server needed.

Note: `list_workflows` is not supported by the time-skipping test server, so `tasks/list` integration tests require a real Temporal server.
