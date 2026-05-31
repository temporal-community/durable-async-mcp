# Client-Side Temporal: Design, Debate, and Decision

## Overview

This document captures the architecture, the options considered, and the reasons for using Temporal on the **client side** of the MCP Tasks protocol. The core conclusion: Temporal on the client side is not a convenience or a pattern preference — it eliminates an entire category of hand-rolled distributed systems infrastructure that a stateless client would need, while simultaneously making the client more correct, more durable, and more extensible to multiple UI types.

The companion diagram is in `client-architecture.excalidraw`.

---

## The MCP Tasks Protocol Is a Client-Side Workflow

The MCP Tasks protocol (SEP-1686) defines a multi-step, stateful, client-side protocol for each in-flight task. For every task, the client must:

1. Call `tools/call` with task metadata → receive `taskId`
2. Poll `tasks/get(taskId)` repeatedly until status leaves `working`
3. On `input_required`:
   - Call `tasks/result(taskId)` — this opens a connection the server uses to send an elicitation
   - Receive elicitation prompt and schema from the server (over that same connection)
   - Present the prompt to a human and wait for their decision
   - Return `ElicitResult` to the server **via the elicitation handler's return value** — the answer travels back over the open `tasks/result` connection, not via a separate call
   - Resume polling `tasks/get`
4. On terminal state (`completed` / `failed` / `cancelled`): call `tasks/result(taskId)` for the final result

> **Protocol note — `tasks/get` does not include elicitation details.** When `tasks/get` returns `input_required`, it only signals that input is needed — it carries no prompt text or schema. Those details only arrive when the client calls `tasks/result` and the server sends the elicitation request back down the same connection. This means the client cannot know *what* to ask the human until it has called `tasks/result` and received the elicitation message. The `handle_elicitation` activity design accounts for this: the activity calls `tasks/result` first, receives the prompt and schema, signals the workflow with those details, and only then waits for the human's decision.

> **Protocol note — `tasks/result` is called twice per task lifecycle.** (1) During elicitation: the connection stays open, the server sends the elicitation, and the answer travels back via the elicitation handler's `ElicitResult` return value. (2) After polling confirms a terminal state: returns the final `CallToolResult`. These are distinct interactions with different purposes, handled by separate activities (`handle_elicitation` and `get_task_result`).

This is a stateful, multi-step protocol with conditional branches, human-in-the-loop, and potentially long waits. It is a workflow. Implementing it without a workflow abstraction means hand-rolling the state machine imperatively.

---

## What a Stateless Client Requires

A stateless client — one that relies on the server for durable state and reconstructs everything in memory on startup — needs all of the following:

### In-session state management
- An in-memory registry mapping `task_id → task state` (polling / awaiting_hitl / done)
- One `asyncio.Task` per tracked MCP task, running a polling loop in the background
- Per-task state for the current polling interval, retry count, and last known status
- Cleanup logic when tasks reach terminal state

### HITL coordination
- A mechanism to detect that a specific task needs human input
- A way to present the elicitation prompt to the user without interleaving with other tasks' output
- A queue to serialize approval prompts when multiple tasks are simultaneously in `input_required`
- Per-task correlation so the user's response goes to the right task's elicitation handler
- A synchronization primitive (e.g. `asyncio.Event`) per task to signal the polling loop that elicitation is complete

### Recovery on restart
- On startup: call `tasks/list` to discover all in-flight tasks — O(N) calls just to know what exists
- Call `tasks/get(taskId)` for each to reconstruct current status — another O(N) calls
- Re-create the polling loop for each task from scratch
- For tasks in `input_required`: re-trigger the elicitation flow (re-call `tasks/result`, re-present the prompt)
- With a large number of tasks, this startup cost is significant and grows with scale

### Connection management
- Logic to detect dropped connections and reconnect
- Reconnection must re-establish the polling state without duplicating work
- For tasks mid-elicitation at the time of a crash: detect that state, re-call `tasks/result`, re-elicit

### What this looks like in code
None of this is provided by any library. It is all custom imperative code: task registries, polling loops, event objects, HITL queues, restart procedures. It is also fragile — a crash anywhere loses all of this state and the restart procedure must be correct to recover cleanly.

---

## Why Not Pure Asyncio?

The asyncio option was evaluated seriously. The argument for it: since the server-side Temporal workflows are durable, a client crash doesn't lose task state — the client just re-queries the server to discover in-flight tasks and restarts tracking them.

This argument is correct but incomplete. The client still needs:
1. The in-session task registry — even if it can be reconstructed, it must exist during the session
2. The O(N) startup recovery procedure
3. All the HITL coordination infrastructure
4. All the connection management logic

The server being durable means the client can *recover* — it does not mean the client needs *less* infrastructure. It means the client's infrastructure has a recovery path. With Temporal, that infrastructure doesn't need to exist at all.

---

## The Case for Temporal on the Client Side

### 1. The task registry is implicit

The set of running `TaskTracker` workflows *is* the task registry. There is no `dict[task_id, state]` to maintain. Temporal holds each task's state in its event history. The workflow's local variables are the state.

### 2. No startup recovery cost

When the Temporal worker restarts, all in-flight `TaskTracker` workflows resume automatically from their last checkpoint. There is no `tasks/list` call, no `tasks/get` loop, no reconstruction of polling state. A workflow that was mid-poll resumes polling. A workflow waiting for HITL approval stays in `wait_condition`. The recovery cost is zero.

### 3. The retry mechanism IS the connection refresh strategy

This is the key insight of the client-side design.

The elicitation handler runs *inside* the activity, tethered to the open `tasks/result` connection. The server is waiting on the other end. The handler must respond before the call can complete. For HTTP transport, that connection has a limited lifetime — a human who takes 20 minutes to respond will outlive any reasonable HTTP timeout.

The solution: set a short `start_to_close_timeout` on the activity (e.g., 10 minutes). When the activity times out, Temporal retries it automatically. The retry calls `tasks/result` again — the server re-elicits because the workflow is still in `PENDING-APPROVAL` — and a fresh connection is established.

There is no custom reconnection logic. No connection pool. No retry-on-failure code. The retry policy *is* the connection lifecycle policy.

### 4. The human decision is decoupled from the activity

The human's approval decision is captured by the workflow via a Temporal signal from the UI. This happens independently of whether any activity is currently executing. The human can respond:
- While an activity is running (the polling handler finds the decision on its next poll)
- While the activity has timed out and Temporal is waiting to retry (the decision is held in workflow state)
- While the activity is between retries (same — held in workflow state)

When the next activity execution polls the workflow query `get_pending_decision`, it finds the answer immediately and the elicitation completes in a single poll cycle. The human's decision survives any number of activity timeouts and retries.

This cleanly separates two concerns that would otherwise be entangled:
- **Connection lifetime**: `activity start_to_close_timeout` — minutes (HTTP connection lifespan)
- **Business SLA**: `workflow.wait_condition timeout` — days (how long we wait for approval)

### 5. Multiple UI types are first-class

Any UI — chat, web, mobile, Slack — signals the workflow by `task_id`. The workflow doesn't know or care which UI sent the signal. Adding a new UI type requires no changes to the workflow or activity layer.

In a stateless client, each UI type would need its own HITL coordination path wired into the shared task registry.

### 6. Temporal UI provides observability

All in-flight client-side tasks are visible in the Temporal UI alongside the server-side workflows. Pending approvals, polling state, and task history are inspectable without adding any instrumentation.

---

## Architecture

### Worker process and the global MCP client

The Temporal worker process owns the `fastmcp.Client` connection. It is initialized once at startup via the **activity class pattern** — the canonical Temporal approach for shared resources (database pools, HTTP clients, LLM clients):

```python
class MCPActivities:
    def __init__(self, client: fastmcp.Client):
        self._client = client

    @activity.defn
    async def start_task(self, invoice_json: dict) -> str: ...

    @activity.defn
    async def poll_task_status(self, task_id: str) -> str: ...

    @activity.defn
    async def handle_elicitation(self, task_id: str) -> str: ...

    @activity.defn
    async def get_task_result(self, task_id: str) -> str: ...


async def main():
    config = load_config(...)
    async with fastmcp.Client(config) as mcp_client:
        temporal_client = await Client.connect("localhost:7233")
        activities = MCPActivities(mcp_client)

        worker = Worker(
            temporal_client,
            task_queue="mcp-client",
            workflows=[TaskTrackerWorkflow],
            activities=[
                activities.start_task,
                activities.poll_task_status,
                activities.handle_elicitation,
                activities.get_task_result,
            ],
        )
        await worker.run()
```

This works for both transports:
- **stdio**: one subprocess, one connection. The worker *is* the session holder.
- **HTTP**: one connection to the MCP server URL. The worker holds it.

JSON-RPC multiplexing (request IDs) means multiple concurrent activities can all use the same `mcp_client` simultaneously without serialization. A request gets a unique ID; responses are matched back by that ID regardless of ordering.

### TaskTracker Workflow

One workflow instance per in-flight MCP task. The workflow owns the state machine for the full client-side task protocol:

```python
@workflow.defn
class TaskTrackerWorkflow:
    def __init__(self):
        self._pending_decision: str | None = None
        self._elicitation_details: ElicitationDetails | None = None

    @workflow.signal
    async def user_decision(self, decision: str):
        self._pending_decision = decision

    @workflow.signal
    async def elicitation_received(self, details: ElicitationDetails):
        self._elicitation_details = details

    @workflow.query
    def get_pending_decision(self) -> str | None:
        return self._pending_decision

    @workflow.query
    def get_elicitation_details(self) -> ElicitationDetails | None:
        return self._elicitation_details

    @workflow.run
    async def run(self, input: TaskTrackerInput) -> str:
        # Phase 1: start the task (if not resuming an existing one)
        task_id = input.task_id or await workflow.execute_activity(
            activities.start_task, input.invoice_json,
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Phase 2: polling loop
        while True:
            status = await workflow.execute_activity(
                activities.poll_task_status, task_id,
                start_to_close_timeout=timedelta(seconds=30),
            )

            if status in TERMINAL_STATES:
                break

            if status == "input_required":
                # Phase 3: elicitation
                await self._handle_elicitation(task_id)
                continue

            await workflow.sleep(timedelta(seconds=POLL_INTERVAL))

        # Phase 4: fetch final result
        return await workflow.execute_activity(
            activities.get_task_result, task_id,
            start_to_close_timeout=timedelta(seconds=30),
        )

    async def _handle_elicitation(self, task_id: str):
        # Activity holds tasks/result connection, gets elicitation details,
        # signals this workflow, then polls get_pending_decision until answered.
        # Activity retries on timeout — each retry refreshes the connection.
        await workflow.execute_activity(
            activities.handle_elicitation, task_id,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                maximum_attempts=0,  # unlimited — retry until decision arrives
                initial_interval=timedelta(seconds=1),
            ),
        )
        # Reset for next potential elicitation round
        self._pending_decision = None
        self._elicitation_details = None
```

### The elicitation activity

This is the heart of the HITL pattern. The activity calls `tasks/result`, which causes the server to send an elicitation. The activity's elicitation handler:

1. Signals the workflow with the prompt and schema (so the workflow can notify the UI)
2. Polls `get_pending_decision` on the workflow while heartbeating
3. Returns `ElicitResult` when the decision is found

```python
@activity.defn
async def handle_elicitation(self, task_id: str) -> str:
    workflow_id = activity.info().workflow_id

    async def elicitation_handler(message, response_type, params, context):
        # Deliver prompt+schema to the workflow (and from there to the UI)
        await temporal_client.get_workflow_handle(workflow_id).signal(
            "elicitation_received",
            ElicitationDetails(
                message=message,
                schema=getattr(params, "requestedSchema", {}),
            ),
        )

        # Poll until the workflow has the human's decision
        while True:
            activity.heartbeat()  # keep Temporal from timing out the activity
            decision = await temporal_client.get_workflow_handle(workflow_id)\
                           .query("get_pending_decision")
            if decision is not None:
                return ElicitResult(action="accept", content={"decision": decision})
            await asyncio.sleep(ELICITATION_POLL_INTERVAL)

    result = await self._client.get_task_result(
        task_id,
        elicitation_handler=elicitation_handler,
    )
    return result
```

**On activity timeout**: the handler is `await`-ing inside the activity. When `start_to_close_timeout` expires, Temporal cancels the activity. The `tasks/result` connection closes. The server's handler is left running (benign — the server-side Temporal workflow is still in `PENDING-APPROVAL`).

**On retry**: a new activity execution calls `tasks/result`. The server sees the workflow still in `PENDING-APPROVAL` and re-elicits. The handler signals the workflow with the same prompt (idempotent — the workflow ignores duplicate details if it already has them). The handler polls `get_pending_decision`. If the human responded between retries, the decision is there immediately and the handler returns in one poll cycle.

**The decoupling**: `workflow.wait_condition` in the UI notification path runs entirely independently. The human can signal the workflow at any moment — while the activity is running, while it has timed out, while Temporal is waiting before the next retry. The decision is captured durably and waits for the next activity execution to collect it.

### Concurrency: how multiple activities share one client

Since activities are `async`, they run as coroutines on the same event loop. Multiple concurrent `poll_task_status` calls all use the same `self._client` simultaneously. JSON-RPC assigns each request a unique ID; the client demultiplexes responses by ID. No serialization is needed. This is the same mechanism used by async HTTP clients, WebSocket clients, and Redis pipelining.

When an activity coroutine is suspended (`await asyncio.sleep(...)` between polls), it yields control back to the event loop. No thread is blocked. The worker remains fully responsive to all other concurrent activities and workflow tasks.

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Worker process owns `mcp_client` | One connection per session; activity class pattern is canonical Temporal for shared resources |
| Short `start_to_close_timeout` on elicitation activity | HTTP connection lifetime; Temporal retry refreshes the connection |
| `wait_condition` timeout is the business SLA | Separates connection concerns from approval deadline concerns |
| Human decision via workflow signal, not elicitation connection | Decision survives activity timeout/retry; any UI type can provide it |
| Activity polls workflow query for decision | Activities cannot receive signals directly; query-poll with heartbeat is the idiomatic pattern |
| Elicitation signal to workflow is idempotent | Multiple retries may re-signal; workflow uses latest or ignores duplicate |
| `maximum_attempts=0` on elicitation retry | Keep retrying indefinitely until the human responds or the workflow's business timeout fires |

---

## Transport Considerations

The design is transport-agnostic. The `fastmcp.Client` abstraction hides whether the underlying connection is stdio or HTTP. The worker owns the client either way.

- **stdio**: pipes have no inherent timeout. The elicitation activity's `start_to_close_timeout` will still fire (driven by Temporal, not the connection), refreshing the interaction if needed. In practice, stdio connections are more robust for long human waits.
- **HTTP**: connections have timeouts at client, server, and proxy layers. The activity timeout + retry pattern handles this correctly without any custom reconnection code.

The "one worker per session" constraint applies strictly to stdio (one subprocess = one connection = one session). For HTTP, the worker could in principle serve multiple users since `tasks/get` and `tasks/result` are stateless with respect to the HTTP session — the `task_id` carries all the context.

---

## Implementation Notes: Divergences from Original Design

Building the actual implementation revealed several places where the original design needed revision. These are not fundamental changes to the architecture, but they are important enough to document.

### 1. `activity.info()` is not available inside the elicitation callback

The design assumed `activity.info().workflow_id` would be accessible inside `_elicitation_handler` since the callback runs while the activity is executing. In practice, FastMCP dispatches the `elicitation/create` handler in a new asyncio task that does not inherit Temporal's context variables (stored in `contextvars.ContextVar`). `activity.info()` raises `RuntimeError: Not in activity context`.

**Fix**: `handle_elicitation` stores `mcp_task_id → tracker_workflow_id` in `_active_elicitations: dict[str, str]` before calling `get_task_result`. The server embeds `x-task-id` in the `requestedSchema` (using `ctx.session.elicit_form()` directly rather than `ctx.elicit()`), and `_elicitation_handler` reads it back to do the lookup. No `activity.info()` needed in the handler.

### 2. The MCP Python SDK `_receive_loop` blocks on the elicitation callback

The design note "JSON-RPC multiplexing means multiple concurrent activities can use the same `mcp_client` simultaneously" is true for *responses* (which are routed by request ID), but not for *incoming server→client requests* like `elicitation/create`.

The MCP Python SDK (`git@github.com:modelcontextprotocol/python-sdk.git`, `mcp/shared/session.py`) awaits `_received_request(responder)` inline — the entire read loop is blocked while the callback runs. Responses to other pending requests (like `start_task`'s `call_tool`) pile up in the receive buffer unread until the callback returns. With a callback that polls for minutes, concurrent `start_task` activities time out and create duplicate server-side workflows.

This is not a spec gap — the MCP specification is silent on how the receive loop is implemented. It is an SDK implementation choice that works for single-task sequential interactions (the original use case) but breaks under concurrent Tasks. Elicitation predates Tasks and this code path was never updated for concurrent use.

**Fix**: `_elicitation_handler` checks `get_pending_decision` **once** (~20ms) and either returns the decision or raises immediately, releasing the reader. Temporal retries `handle_elicitation` — brief turns on the reader rather than holding it indefinitely. `maximum_interval=10s` caps the backoff so the approval is processed within at most 10 seconds of the user deciding.

The SDK fix is one line: `task_group.start_soon(self._received_request, responder)` instead of `await self._received_request(responder)`.

### 3. `_make_wrapped_call_tool` `is_task` detection was broken

The original handler checked `ctx.experimental.is_task` to determine whether a `process_invoice` call was task-augmented. This check always failed (returning `False`), causing all calls to fall through to FastMCP's default task handling (Docket/Redis), which is not configured. FastMCP would hang waiting for Docket — while also creating an InvoiceWorkflow, explaining why "invoices were being created but `start_task` timed out."

**Fix**: The `is_task` guard is removed. Since `process_invoice` has `task=TaskConfig(mode="required")`, all calls to it are task-augmented by definition. The handler always intercepts.

### 4. Cancel-after-elicitation is required but asyncio.create_task was initially unsafe

The design specified cancelling `tasks/result` after elicitation per MCP spec, using `asyncio.create_task`. This was correct in principle but initially broke because `activity.info()` (needed in the handler) wasn't available in the created task — leading to the polling-inline approach that caused the reader-blocking problem.

Once fix #1 eliminated the need for `activity.info()` in the handler, cancel-after-elicitation via `asyncio.create_task` became safe and was restored. `handle_elicitation` now: creates a background task for `get_task_result`, waits for the elicitation event OR task completion, and cancels the task if elicitation was handled — releasing the server connection promptly.
