# Plan: Fix handle_elicitation Connection Lifetime

> **⚠️ Historical / superseded.** Fixes a problem — the blocking `tasks/result` + `handle_elicitation`
> connection lifetime — that **no longer exists** in v2 (no `tasks/result`, no server-initiated
> elicitation). See [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md) and
> [`mcp_tasks_temporal/`](../../mcp_tasks_temporal/README.md). Kept for history; the v2 approach is the
> *current experiment*, not a permanent move off FastMCP.

## Context

`handle_elicitation` currently awaits `self._mcp.get_task_result(task_id)` directly, which blocks until the entire InvoiceWorkflow completes (PAID/FAILED/REJECTED). The MCP server's `tasks/result` handler signals the workflow, then calls `await handle.result()` waiting for terminal state — the client's connection stays open this entire time.

With N concurrent tasks all in `input_required` state simultaneously, there are N long-lived open connections to the shared MCP server subprocess. When another `start_task` activity tries to use the same client, it competes for the connection — and times out at 30 seconds, creating orphaned InvoiceWorkflows on each retry.

The MCP spec explicitly permits and recommends cancelling `tasks/result` after elicitation and resuming polling. The old `mcp_client/main.py` already does this correctly. We deviated from that pattern because `asyncio.create_task` broke `activity.info()` in `_elicitation_handler`. Now that the handler uses `_active_elicitations` dict lookup (no `activity.info()` needed), we can safely return to the cancel-after-elicitation pattern.

## Changes

### `async_mcp/client_worker/activities.py`

**Add `_elicitation_events: dict[str, asyncio.Event]`** alongside `_active_elicitations` in `__init__`.

**`_elicitation_handler`**: after finding the decision, set `_elicitation_events[task_id]` before returning `ElicitResult`. Setting the event first (before the return) ensures the event fires while the handler is still "live" — FastMCP processes the return value synchronously before the event loop runs the cancellation.

```python
if decision is not None:
    event = self._elicitation_events.get(task_id)
    if event:
        event.set()
    return ElicitResult(action="accept", content={"value": decision})
```

**`handle_elicitation`**: switch back to `asyncio.create_task` + cancel pattern:

```python
@activity.defn
async def handle_elicitation(self, task_id: str) -> str:
    self._active_elicitations[task_id] = activity.info().workflow_id
    elicitation_resolved = asyncio.Event()
    self._elicitation_events[task_id] = elicitation_resolved
    try:
        result_task = asyncio.create_task(self._mcp.get_task_result(task_id))
        resolved_sentinel = asyncio.create_task(elicitation_resolved.wait())

        done, _ = await asyncio.wait(
            {result_task, resolved_sentinel},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if elicitation_resolved.is_set():
            # Per MCP spec: cancel tasks/result after elicitation, resume polling.
            result_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await result_task
            resolved_sentinel.cancel()
            return "elicitation_handled"
        else:
            # result_task finished first (server returned without eliciting, or error)
            resolved_sentinel.cancel()
            result_task.result()  # re-raise any exception
            return "completed"
    finally:
        self._active_elicitations.pop(task_id, None)
        self._elicitation_events.pop(task_id, None)
```

**Why cancellation is safe now**: `_elicitation_handler` returns `ElicitResult` synchronously after setting the event (no `await` between `event.set()` and `return`). FastMCP processes the return value (writes the response to the server) at the point where `result_task` yields control — that write is already in-flight before `result_task.cancel()` fires.

**Why `asyncio.create_task` is safe now**: `_elicitation_handler` uses `_active_elicitations[task_id]` for the workflow_id lookup (no `activity.info()`). Tasks created by FastMCP's internal dispatch don't need Temporal context.

### `async_mcp/tests/test_mcp_activities.py`

Update `TestHandleElicitation`:
- `test_returns_completed_after_elicitation` → rename `test_returns_elicitation_handled_after_elicitation`, assert result `== "elicitation_handled"`
- `test_registers_and_clears_active_elicitation` → also check `_elicitation_events` is cleared in finally
- Add: `test_returns_completed_when_result_task_finishes_first` (no elicitation case)

Update `TestElicitationHandler`:
- `test_signals_workflow_with_elicitation_details` → verify event is set after decision returned

## Verification

1. `uv run pytest async_mcp/tests/` — all tests pass
2. Start three task-tracker workflows concurrently; approve all three — no orphaned workflows, all three task trackers complete, `start_task` never times out
