# Plan: Client-Side Temporal Worker for MCP Task Management

## Context

The existing `async_mcp/mcp_client/` is a stateless LLM-driven CLI that hand-rolls polling loops and elicitation handling in Python. It has no durability guarantees: crashes lose task state, reconnection requires custom code, and human-in-the-loop approval is coupled to the current process.

We're replacing this with a Temporal-based client-side worker (`async_mcp/client_worker/`). One `TaskTrackerWorkflow` per MCP task durably owns the full task lifecycle — polling, elicitation, retry-on-timeout. Activities hold the shared MCP client connection. A separate UI process connects to Temporal only (no MCP) and communicates via signals/queries.

No changes to the MCP server (`async_mcp/server.py`, `async_mcp/temporal_task_handlers.py`).

---

## Files to Create

```
async_mcp/client_worker/
├── __init__.py
├── __main__.py       # entry point dispatcher: --worker or --ui
├── models.py         # ElicitationDetails, TaskTrackerInput
├── workflows.py      # TaskTrackerWorkflow
├── activities.py     # MCPActivities
├── worker.py         # worker startup
└── ui.py             # interactive CLI UI (Temporal-only, no MCP)

async_mcp/tests/
├── test_task_tracker_workflow.py
└── test_mcp_activities.py
```

---

## Data Models (`models.py`)

```python
@dataclass
class ElicitationDetails:
    message: str
    schema: dict

@dataclass
class TaskTrackerInput:
    invoice_json: dict
    task_id: str | None = None  # None = start new task; set = resume existing
```

Both must be serializable for Temporal (use `dataclass_json` or simple dicts).

---

## `TaskTrackerWorkflow` (`workflows.py`)

One instance per MCP task. Owns the full client-side state machine.

**Instance state:**
- `_pending_decision: str | None` — set by `user_decision` signal, cleared after elicitation handled
- `_elicitation_details: ElicitationDetails | None` — set by `elicitation_received` signal, cleared after handled

**Signals:**
- `user_decision(decision: str)` — UI calls this when human approves/rejects
- `elicitation_received(details: ElicitationDetails)` — `handle_elicitation` activity calls this when server sends elicitation prompt

**Queries:**
- `get_pending_decision() -> str | None` — `handle_elicitation` activity polls this to detect human response
- `get_elicitation_details() -> ElicitationDetails | None` — UI polls this to know what to show

**Execution flow:**
```python
async def run(self, input: TaskTrackerInput) -> str:
    # Phase 1: start or resume
    task_id = input.task_id or await workflow.execute_activity(
        MCPActivities.start_task, input.invoice_json,
        start_to_close_timeout=timedelta(seconds=30))

    # Phase 2: poll + elicitation loop
    while True:
        status = await workflow.execute_activity(
            MCPActivities.poll_task_status, task_id,
            start_to_close_timeout=timedelta(seconds=30))

        if status in ("completed", "failed", "cancelled"):
            return await workflow.execute_activity(
                MCPActivities.get_task_result, task_id,
                start_to_close_timeout=timedelta(seconds=30))

        if status == "input_required":
            await workflow.execute_activity(
                MCPActivities.handle_elicitation, task_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=0))
            # Reset elicitation state for any future elicitation
            self._pending_decision = None
            self._elicitation_details = None
            continue  # resume polling

        await workflow.sleep(timedelta(seconds=2))
```

**Key properties:**
- Workflow ID: `task-tracker-{mcp_task_id}` — avoids collision with server-side InvoiceWorkflow IDs
- Task queue: `client-task-queue`

---

## `MCPActivities` (`activities.py`)

Holds the single shared `fastmcp.Client` connection (one per worker process). Constructed with both the MCP client and Temporal client. The `elicitation_handler` is a bound method that uses `activity.info().workflow_id` to route to the correct `TaskTrackerWorkflow`.

### Activity 1: `start_task(invoice_json: dict) -> str`
```python
task = await self._mcp.call_tool("process_invoice", {"invoice": invoice_json})
return task.id  # MCP task ID = server-side InvoiceWorkflow ID
```

### Activity 2: `poll_task_status(task_id: str) -> str`
```python
status = await self._mcp.get_task_status(task_id)
return status.status.state  # "working", "input_required", "completed", "failed"
```

### Activity 3: `handle_elicitation(task_id: str) -> str`
This is the complex one. Calls `tasks/result`, handles the elicitation, then **cancels the connection** (per MCP spec: client cancels after elicitation, resumes polling). Returns `"elicitation_handled"`.

```python
# Workflow resumes from context
workflow_id = activity.info().workflow_id
elicitation_resolved = asyncio.Event()

# Store event so _elicitation_handler can signal it
self._elicitation_events[workflow_id] = elicitation_resolved

try:
    result_task = asyncio.create_task(self._mcp.get_task_result(task_id))
    # Wait for elicitation callback to complete OR task to finish on its own
    done, _ = await asyncio.wait(
        {result_task, asyncio.create_task(elicitation_resolved.wait())},
        return_when=asyncio.FIRST_COMPLETED)
    if elicitation_resolved.is_set():
        result_task.cancel()           # cancel per MCP spec
        with suppress(asyncio.CancelledError, Exception): await result_task
        return "elicitation_handled"
    else:
        return "completed"             # edge case: server resolved without eliciting
finally:
    self._elicitation_events.pop(workflow_id, None)
```

### `_elicitation_handler(elicitation) -> ElicitResult`
Set at Client construction time. Called by FastMCP when server triggers `ctx.elicit()`.

```python
workflow_id = activity.info().workflow_id           # requires same asyncio context
handle = self._temporal.get_workflow_handle(workflow_id)
details = ElicitationDetails(message=str(elicitation.message), schema=...)
await handle.signal(TaskTrackerWorkflow.elicitation_received, details)

while True:
    activity.heartbeat()                              # keep Temporal alive
    decision = await handle.query(TaskTrackerWorkflow.get_pending_decision)
    if decision:
        self._elicitation_events[workflow_id].set()   # unblock handle_elicitation
        return ElicitResult(action="accept", content=[{"type": "text", "text": decision}])
    await asyncio.sleep(0.5)
```

**Why `activity.info()` works here:** `contextvars.ContextVar` is inherited by coroutines awaited within the same task. FastMCP calls the handler as a coroutine within the activity's async context, so `activity.info()` resolves correctly.

### Activity 4: `get_task_result(task_id: str) -> str`
```python
result = await self._mcp.get_task_result(task_id)
return json.dumps(result)   # serialize CallToolResult for Temporal
```

---

## `worker.py`

```python
async def run():
    config = load_config("async_mcp/client_config.json")
    server_params = config["mcpServers"]["invoice-processor"]
    temporal_client = await Client.connect(temporal_address)

    # Create activities first so we have the bound handler reference
    acts = MCPActivities(mcp_client=None, temporal_client=temporal_client)

    async with fastmcp.Client(server_params, elicitation_handler=acts._elicitation_handler) as mcp:
        acts._mcp = mcp
        worker = Worker(temporal_client, task_queue="client-task-queue",
                        workflows=[TaskTrackerWorkflow],
                        activities=[acts.start_task, acts.poll_task_status,
                                    acts.handle_elicitation, acts.get_task_result])
        await worker.run()
```

---

## `ui.py` (V1: one elicitation at a time)

Separate process. Connects to Temporal only. No MCP dependency.

**Entry point behavior:**
```
Invoice Processor
> Commands: submit <file>, list, quit
> submit samples/invoice1.json
  Started: task-tracker-wf-abc123

[polls all running task-tracker-* workflows for pending elicitations]
[finds one in input_required state]

Approval needed for invoice INV-001:
  Customer: Acme Corp
  Total: $1,250.00
  Lines: 2

Decision [approve/reject]: approve
  Sent decision. Resuming...

> list
  task-tracker-wf-abc123  PAYING (working)
  task-tracker-wf-def456  PAID (completed)
```

**V1 scoping:** The UI polls all running `TaskTrackerWorkflow` instances (via Temporal `list_workflows` filtered by workflow type). It handles at most one pending elicitation at a time — the first it finds. Further elicitation UX polish is deferred.

**Key UI operations (all via Temporal, no MCP):**
- `temporal_client.start_workflow(TaskTrackerWorkflow, input, id=...)` — to start a task
- `workflow_handle.query(TaskTrackerWorkflow.get_elicitation_details)` — to detect pending elicitations
- `workflow_handle.signal(TaskTrackerWorkflow.user_decision, decision)` — to send approval
- `temporal_client.list_workflows(f'WorkflowType="TaskTrackerWorkflow"')` — to list all tasks

---

## Tests

### `test_task_tracker_workflow.py`

Use `WorkflowEnvironment.start_time_skipping()`. Mock all activities via `@activity.defn` stubs passed to the test worker.

| Test | Setup | Assert |
|------|-------|--------|
| starts new task when no task_id given | mock `start_task` returns "task-abc" | `start_task` called once with invoice_json |
| skips start when task_id provided | input has task_id set | `start_task` never called |
| polls until terminal | `poll_task_status` → `"working"`, `"working"`, `"completed"` | workflow returns get_task_result value |
| handles input_required state | `poll_task_status` → `"input_required"` then `"completed"`, `handle_elicitation` mocked | `handle_elicitation` called once, then polling resumes |
| `get_elicitation_details` query | signal `elicitation_received(details)` to running workflow | query returns matching details |
| `user_decision` signal | signal `user_decision("approve")` | query `get_pending_decision` returns `"approve"` |
| elicitation state cleared after loop | signal decision, activity returns, poll continues | `_pending_decision` and `_elicitation_details` are None after continue |

### `test_mcp_activities.py`

Unit tests for each activity with `AsyncMock` for `fastmcp.Client` and `temporalio.client.Client`.

Simple activities (`start_task`, `poll_task_status`, `get_task_result`) can be tested directly with `ActivityEnvironment()`.

`handle_elicitation` requires a `WorkflowEnvironment` test because it calls `activity.info().workflow_id` inside a callback. Set up a minimal workflow that:
1. Starts `handle_elicitation` activity
2. Signals `user_decision` after elicitation details arrive
3. Asserts activity returns `"elicitation_handled"`

---

## Implementation Unknowns to Verify Early

1. **FastMCP Client API for task-enabled tools**: Confirm `call_tool("process_invoice", ...)` returns an object with `.id`. Check if `task=True` arg is needed or if tool metadata handles it.
2. **`activity.info()` in elicitation callback**: Verify `contextvars` are propagated through FastMCP's internal callback dispatch (should work with awaited coroutines, but confirm).
3. **FastMCP `get_task_result` cancellation**: Confirm `asyncio.create_task(...).cancel()` cleanly closes the stdio subprocess read, not just the Python coroutine.

Check these by reading `fastmcp` source early in the implementation: `fastmcp/client/client.py` for the API, `fastmcp/client/elicitation.py` for handler dispatch.

---

## Verification

1. `uv run pytest async_mcp/tests/test_task_tracker_workflow.py` — all workflow tests pass
2. `uv run pytest async_mcp/tests/test_mcp_activities.py` — all activity tests pass  
3. `uv run pytest async_mcp/tests/` — existing tests still pass (no regressions)
4. End-to-end: start Temporal server + bizservice worker + client worker, then submit invoice via UI, approve it, see PAID result
