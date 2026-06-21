# Activity Signaling Its Own Workflow: Pattern and Rationale

> **⚠️ Historical / superseded.** Documents the `_elicitation_handler` workaround (an activity signaling
> its own workflow) needed for server-initiated elicitation under the 2025-11-25 spec. That whole
> mechanism is gone in v2 — HITL is pull-based (`tasks/get` `inputRequests` → `tasks/update`). See
> [`mcp_tasks_temporal/`](../../mcp_tasks_temporal/README.md) and
> [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md). Kept for history; the v2 approach is the
> *current experiment*, not a permanent move off FastMCP.

## The Pattern

In `async_mcp/client_worker/activities.py`, the `_elicitation_handler` callback (called from within the `handle_elicitation` activity) reaches *up* into its own parent workflow:

```python
# activities.py — inside _elicitation_handler
workflow_id = self._active_elicitations.get(task_id)   # resolves to the TaskTrackerWorkflow ID
handle = self._temporal.get_workflow_handle(workflow_id)

await handle.signal("elicitation_received", details)    # activity → its own workflow
decision = await handle.query("get_pending_decision")   # activity ← its own workflow
```

The workflow ID is obtained via `activity.info().workflow_id` (captured earlier in `handle_elicitation` and stored in `_active_elicitations` before the callback context changes — see [implementation notes](client-side-temporal.md#implementation-notes-divergences-from-original-design)).

## Why This Is Unusual

The typical Temporal HITL pattern goes in the opposite direction:

1. Activity returns early (or raises a specific signal value) indicating input is needed
2. Workflow enters `wait_condition` or `workflow.wait_condition`
3. External entity (UI, human, system) signals the workflow directly
4. Workflow resumes and calls a subsequent activity to act on the input

In that pattern, activities are passive — they do work and return. The workflow orchestrates the wait. External entities communicate directly with the workflow, not through activities.

Our pattern inverts this: the activity reaches up into the workflow, surfaces the elicitation prompt, and polls for the result.

## Why We're Forced Into It

The MCP elicitation callback contract is the constraint. `_elicitation_handler` is a callback registered with `fastmcp.Client`. When the server sends `elicitation/create`, FastMCP invokes this callback and **requires it to return an `ElicitResult`** — the server is waiting synchronously for that return value. There is no way to break out of the callback, return early, and have the workflow deliver the response later.

This means the decision *must* be delivered from within the callback. The callback runs within the activity's execution context. Therefore the activity is the only entity that can complete the round-trip: receive prompt from server → surface to UI → collect human response → return to server.

## The More Idiomatic Alternative (If Unconstrained)

Without the MCP elicitation callback contract, the idiomatic design would be:

1. `handle_elicitation` activity detects `input_required`, surfaces details to the workflow via heartbeat or a special return value
2. Workflow stores the elicitation details and enters `wait_condition(lambda: self._pending_decision is not None)`
3. UI queries `get_elicitation_details`, presents prompt, signals `user_decision`
4. Workflow resumes, calls a `deliver_decision` activity that signals the InvoiceWorkflow directly via Temporal

Temporal's **Update** feature (a validated signal that blocks until the handler returns a result) would make this even cleaner — the UI could issue an Update that atomically provides the decision and confirms delivery.

## SDK Support

`activity.info().workflow_id` is an explicit part of the Temporal Python SDK's public API, so the pattern is supported. It is uncommon in practice — most examples of activities communicating with workflows use heartbeat (for progress) or standard return values. Querying or signaling the *parent* workflow from within an activity is valid but not a documented best practice.
