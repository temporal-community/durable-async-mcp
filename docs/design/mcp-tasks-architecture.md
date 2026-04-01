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
  |<-- elicitation: approve? -----|  (ctx.elicit() within      |
  |-- user responds: approve ---->|   tasks/result handler)    |
  |                               |-- signal ApproveInvoice -->|
  |                               |-- handle.result() -------->|
  |                               |<-- "PAID" -----------------|
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
4. Awaits the workflow result
5. Returns a `CallToolResult` with the terminal status

```python
response = await ctx.elicit(
    message=f"Invoice {invoice_id} for {customer} (${total_amount:.2f}) ...",
    response_type=["approve", "reject"],
)
```

Both `decline` and `cancel` actions map to a workflow rejection signal.

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
