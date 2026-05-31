# MCP Tasks Protocol Gaps (SEP-1686)

Research notes from investigating the MCP Tasks specification (experimental, as of 2026-03).

## Gap 1: `tasks/list` Has No Filtering or Tool Association

### Problem

The `Task` model in the spec has no field indicating which tool created it. The `ListTasksRequest` has no filter parameters — only a pagination cursor. If a server has multiple task-enabled tools, `tasks/list` returns a flat, untyped list with no way to scope by tool.

### Impact

- Clients must infer task origin from context (e.g., `statusMessage`, `taskId` naming convention, or by calling `tasks/get` and inspecting results).
- Becomes a real usability problem the moment a server has 2+ task-enabled tools.
- No public discussion found on this gap in the spec repo as of 2026-03-23.

### Task Model Fields (from `mcp/types.py`)

```
taskId, status, statusMessage, createdAt, lastUpdatedAt, ttl, pollInterval
```

No `toolName`, `type`, or similar field exists.

### Possible Mitigations

- **`_meta` on result envelopes (spec-endorsed):** `ListTasksResult` extends `Result`, which has a `_meta` field. The [spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/index#_meta) says `_meta` is intended for exactly this kind of protocol-level extensibility. A server could include a tool-to-task mapping in `_meta`:
  ```json
  {
    "_meta": {
      "taskTools": {
        "invoice-abc123": "process_invoice",
        "refund-xyz789": "process_refund"
      }
    },
    "tasks": [...]
  }
  ```
  Similarly, `GetTaskResult` extends both `Result` and `Task`, so individual `tasks/get` responses also carry `_meta`. The limitation is that `_meta` lives on the result envelope, not on individual `Task` objects within the `tasks` array — so the metadata is separated from the tasks it describes.
- **Convention-based:** encode tool name in `taskId` (e.g., `invoice-<uuid>`, `refund-<uuid>`)
- **`statusMessage`:** include tool/workflow context in the human-readable status message.
- **Protocol-level fix:** propose adding a `toolName` field to `Task` and filter params to `ListTasksRequest`.

## Gap 2: `tasks/result` Must Block Until Terminal State

### What the Spec Says

> "When a receiver receives a `tasks/result` request for a task in any other non-terminal status (`working` or `input_required`), it **MUST** block the response until the task reaches a terminal status."

While blocked, the server delivers queued messages (elicitation, sampling) as side-channel messages within the response stream. Multiple `working <-> input_required` cycles are allowed within a single blocked `tasks/result` call.

### Problem

For long-running workflows, "block until terminal" means potentially unbounded connection hold times. Our `InvoiceWorkflow` waits up to 5 days for approval. If a client calls `tasks/result` on a `working` task early, that connection must stay open for days.

The spec assumes clients will be smart about *when* they call `tasks/result` (only after seeing `input_required` or terminal via polling), but making it a MUST-block rather than allowing an error response means misbehaving or eager clients get silently stuck.

If the connection drops mid-block, the task keeps running (Temporal is durable), but the MCP protocol loses its handle. The client must rediscover the task via `tasks/list` and call `tasks/result` again.

### Our Divergence

Our `handle_tasks_result` in `temporal_task_handlers.py` (line 191-198) intentionally raises `McpError` for `working` states instead of blocking. This is technically non-compliant but more practical — it fails fast and lets the client decide what to do.

### Proposed Spec Fix

SEP-2322 ("Multi Round-Trip Requests") proposes a stateless `IncompleteResult` response pattern that would allow `tasks/result` to return without blocking, sending the client back to polling. This would address both the long-lived connection problem and improve resilience to connection failures.

## Gap 3: Elicitation Was Designed Before Tasks — Sequential Reader Breaks Under Concurrent Tasks

### Root Cause

Elicitation and Tasks were designed independently. Elicitation assumes a single-threaded, sequential client: one agent, one conversation, one interaction at a time. When `elicitation/create` arrives, the client handles it and responds before anything else happens. The MCP Python SDK's `_receive_loop` in `mcp/shared/session.py` reflects this assumption — it `await`s `_received_request(responder)` **inline**, blocking the reader until the handler returns:

```python
# mcp/shared/session.py — the receive loop for server→client requests
await self._received_request(responder)   # blocks entire read loop
```

Inside `_received_request`, the elicitation callback is called with `await`:

```python
# mcp/client/session.py
case types.ElicitRequest(params=params):
    with responder:
        response = await self._elicitation_callback(ctx, params)
        await responder.respond(client_response)
```

While the callback runs, the reader loop cannot process any other incoming messages — including responses to other pending client requests.

### Why Tasks Breaks This

Tasks introduces async, multiple concurrent in-flight operations. A Temporal-backed client worker managing N concurrent `TaskTrackerWorkflow`s will have N concurrent `handle_elicitation` activities, each calling `tasks/result` on the shared MCP client. When the server responds to any of these with `elicitation/create`, the reader blocks for the duration of the callback.

If the callback polls for a human decision (which can take minutes), all other pending requests — including `start_task`'s `call_tool` for a different task — sit unread in the receive buffer. Activities time out. Workflows create duplicate server-side work on retry.

This is not a performance problem or a slow-handler problem. It is a concurrency model mismatch: the sequential receive loop was designed for a world where elicitation happens one at a time. Tasks makes concurrent elicitations structurally possible.

### Impact

- Any MCP client implementation that handles Tasks concurrently will hit this if multiple tasks reach `input_required` simultaneously.
- The issue is in `mcp/shared/session.py` (the base SDK), not in FastMCP. FastMCP could work around it by overriding `_received_request` to dispatch with `anyio.create_task_group().start_soon()`, but hasn't.
- The blocking is proportional to how long the elicitation handler runs. For LLM clients (seconds), it's invisible. For human-in-the-loop clients (minutes), it's a hard blocker.

### Our Workaround

`_elicitation_handler` in `async_mcp/client_worker/activities.py` checks for a pending decision **once** (~20ms) and either returns the decision or raises immediately. The MCP reader is released after each attempt. Temporal's retry mechanism (capped at 10s backoff) provides the polling cadence — the handler takes brief turns on the reader rather than holding it.

```python
# Check once, release the reader either way
decision = await handle.query("get_pending_decision")
if decision is not None:
    return ElicitResult(action="accept", content={"value": decision})
raise RuntimeError("No decision yet; handle_elicitation will retry")
```

### Proposed Protocol Fix

The MCP SDK should dispatch incoming server→client requests in separate tasks:

```python
# Instead of: await self._received_request(responder)
task_group.start_soon(self._received_request, responder)
```

This is a one-line change in `mcp/shared/session.py` that would make the receive loop non-blocking for all incoming server requests, regardless of how long the handler takes. It is the standard pattern for async request servers and would make elicitation composable with concurrent Tasks.

## Synthesis: These Challenges Are Not Temporal-Specific

The gaps above surface specific MCP protocol design issues, but the underlying challenges are inherent to building any serious MCP Tasks client — Temporal or otherwise. A client built with hand-rolled asyncio, threads, or any other concurrency primitive would face every one of them.

**Sequential reader blocking (Gap 3):** Any concurrent-task client hits this. Without Temporal you'd implement the same check-once-and-retry logic with `asyncio.sleep` loops, `threading.Event`s, or similar. Same problem, more hand-rolled machinery.

**Connection lifetime (Gap 2):** `tasks/result` holding a connection open while a human decides (minutes to hours) is a protocol design issue. Any client needs a strategy: hold it open and risk timeouts, or cancel-and-retry. Temporal's retry mechanism *is* that strategy, but you'd build the same explicit retry loops and backoff logic without it.

**Elicitation routing (the `x-task-id` problem):** The elicitation handler not knowing which task triggered it is a protocol gap. Any concurrent client needs this correlation. Without Temporal you'd still build a dict mapping task IDs to waiting state — you'd just store it in Redis or in-memory rather than implicitly in workflow instance variables.

**Human-in-the-loop durability:** If the process crashes while waiting for approval, how do you recover? Without Temporal you'd build explicit state persistence: write "waiting for approval on task X" to a database on entry, query `tasks/get` on restart to discover in-flight tasks, rebuild in-memory state. Temporal's workflow history *is* that persistence layer.

**At-least-once delivery:** `start_task` creating duplicate server-side workflows on retry is a classic problem. Without Temporal retries you'd still need to handle "my request timed out but the server may have processed it." The fix (deterministic task IDs) is protocol-level regardless of client implementation.

**What Temporal actually provides** is a vocabulary for expressing the solutions cleanly. Signals and queries replace custom pub-sub. Retries replace manual try/except/sleep loops. Workflow history replaces a state database. The per-task state machine is just the workflow definition rather than an explicit FSM with a hand-written persistence layer.

The demo makes this point implicitly — which is its core thesis: the complexity budget for building a correct, durable, recoverable MCP Tasks client is non-trivial whether or not you use Temporal. Temporal lets you spend that budget on the problem rather than the infrastructure.

## References

- [SEP-1686: Tasks Issue](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686)
- [SEP-1686: Tasks PR](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1732)
- [2026 MCP Roadmap](https://modelcontextprotocol.io/development/roadmap)
