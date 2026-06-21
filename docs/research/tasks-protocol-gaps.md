# MCP Tasks Protocol Gaps (SEP-1686)

> **Note (predates v2).** Documents gaps in the 2025-11-25 Tasks feature (SEP-1686). The 2026-07-28
> redesign resolves several: blocking `tasks/result` removed, server-initiated elicitation gone (no
> sequential-reader bottleneck), `tasks/list` removed. Still a valid analysis of the older spec — see
> [`mcp-2026-07-28-spec-impact.md`](mcp-2026-07-28-spec-impact.md) and
> [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md).

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

## Gap 3: Elicitation Was Designed Before Tasks — Concurrent Tasks Expose a Python SDK Implementation Limitation

### Note on "gap"

The MCP *specification* is silent on how the receive loop is implemented — it defines protocol messages, not runtime behavior. This is therefore not a spec gap but an **implementation gap in the MCP Python SDK** (`git@github.com:modelcontextprotocol/python-sdk.git`). A spec-compliant implementation could handle this correctly; the current Python SDK implementation happens not to.

### Precise framing

The issue is within a single client session: the receive loop processes incoming server→client *requests* (like `elicitation/create`) sequentially. It is not about multiple sessions being queued — our architecture uses one shared session (one stdio subprocess). Within that session, each `elicitation/create` blocks the loop until its handler returns, preventing any other buffered messages from being processed.

### The Implementation Issue

The MCP Python SDK's `_receive_loop` in `mcp/shared/session.py` dispatches incoming server→client requests (like `elicitation/create`) inline with `await`:

```python
# modelcontextprotocol/python-sdk — mcp/shared/session.py
await self._received_request(responder)   # blocks entire read loop
```

Inside `_received_request`, the elicitation callback is also called with `await`:

```python
# mcp/client/session.py
case types.ElicitRequest(params=params):
    with responder:
        response = await self._elicitation_callback(ctx, params)
        await responder.respond(client_response)
```

While the callback runs, the reader loop cannot process any other incoming messages — including responses to other pending client requests. This is an implementation choice, not a protocol requirement.

### Why Concurrent Tasks Surface This

Tasks introduces async, multiple concurrent in-flight operations. A Temporal-backed client worker managing N concurrent `TaskTrackerWorkflow`s will have N concurrent `handle_elicitation` activities, each calling `tasks/result` on the shared MCP client. When the server responds to any of these with `elicitation/create`, the reader blocks for the duration of the callback.

If the callback polls for a human decision (which can take minutes), all other pending requests — including `start_task`'s `call_tool` for a different task — sit unread in the receive buffer. Activities time out. Workflows create duplicate server-side work on retry.

This is not a performance problem or a slow-handler problem. It is a concurrency model mismatch: the sequential inline dispatch was written assuming one interaction at a time. Elicitation predates Tasks; when Tasks was added, this assumption was not revisited.

### Impact

- Any MCP client implementation that handles Tasks concurrently will hit this if multiple tasks reach `input_required` simultaneously.
- FastMCP builds on top of the Python SDK and inherits this behavior. FastMCP could work around it by overriding `_received_request` to dispatch with `anyio.create_task_group().start_soon()`, but hasn't.
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

### Proposed SDK Fix

**Issue**: [modelcontextprotocol/python-sdk#2489](https://github.com/modelcontextprotocol/python-sdk/issues/2489) — describes the sequential inline dispatch limitation ("Peak in-flight handlers = 1 regardless of fan-out").

**PR in progress**: [modelcontextprotocol/python-sdk#2490](https://github.com/modelcontextprotocol/python-sdk/pull/2490) ("fix: dispatch client request handlers concurrently", Closes #2489) — open as of 2026-05-31.

The PR introduces an opt-in `_dispatch_requests_concurrently` flag on `BaseSession`. When set, request handlers are spawned with `task_group.start_soon()` instead of being awaited inline. `ClientSession` enables this flag (parallel execution), while `ServerSession` keeps it off to preserve its initialization state machine ordering.

The change is more involved than a one-line swap: concurrent dispatch exposed two race conditions in `RequestResponder` that the PR also fixes — a cancel-before-enter race and an idempotent cancellation issue. Handler exceptions are also now caught and translated to JSON-RPC error responses rather than terminating the session.

The net effect for our use case: `ClientSession` (which is what `fastmcp.Client` uses) gets non-blocking dispatch of incoming server→client requests. `_elicitation_handler` can poll for minutes without blocking other in-flight requests.

### Transport Dependency: Does This Apply to Streamable HTTP?

This bottleneck is **confirmed for stdio transport** (our current setup) where there is one subprocess, one pipe, and one shared `_read_stream` feeding the receive loop.

For **streamable HTTP** the answer depends on how the transport is wired:

- **Shared SSE channel**: if the client maintains one persistent GET connection for all server-initiated messages, all `elicitation/create` requests arrive on that single stream — same sequential bottleneck applies.

- **Per-request response streams**: if each client HTTP POST (`tasks/result`) gets its own independent response stream and the server sends `elicitation/create` back on that specific stream, the streams are isolated. Handler A blocking on its stream would not affect handler B's stream. The sequential receive loop problem would not apply and the 20ms workaround would be unnecessary overhead.

The MCP streamable HTTP spec permits either model. The Python SDK's HTTP transport implementation would need to be checked to determine which applies. If it uses isolated per-request streams, concurrent tasks over HTTP would not hit this problem.

### Our Workaround (stdio) and What It Actually Does

The 20ms check-once-and-raise pattern achieves concurrency-like behavior by rotating through pending elicitations: each handler takes a brief turn on the receive loop (~20ms), yields if the human hasn't responded yet, and Temporal retries after a short backoff. When a retry fires and the answer is ready, the handler completes immediately. The receive loop is free between retries to process other buffered messages (responses to `start_task`, `poll_task_status`, etc.).

Temporal workflow state (`_pending_decision`, `_active_elicitations`, `x-task-id` routing) preserves enough context across retries that each resumption is seamless — the server re-elicits idempotently and the handler either finds the answer or yields again.

### Impact on Our Design If the SDK Is Fixed

The raise-and-retry pattern in `_elicitation_handler` is a workaround for this SDK limitation. With the fix, `_elicitation_handler` can poll with `asyncio.sleep(0.5)` for as long as needed without starving other activities — the original intended design.

**What stays the same:**
- `_active_elicitations` dict still required — FastMCP still dispatches the handler in a new asyncio task, so `activity.info()` is still unavailable there; routing via `x-task-id` still needed
- Cancel-after-elicitation still needed — `handle_elicitation` still shouldn't hold the `tasks/result` connection open waiting for the full InvoiceWorkflow to complete
- `start_to_close_timeout` on `handle_elicitation` still appropriate — HTTP connection timeouts and worker crashes can still interrupt a long-running activity; on retry, the decision is already in `_pending_decision` and the handler finds it immediately

**What changes:**
- The `raise RuntimeError("No decision yet")` pattern and `maximum_interval` retry cap become unnecessary
- The handler simplifies back to a polling loop
- The `handle_elicitation` activity runs for potentially minutes rather than failing fast and retrying

**Demo impact:**

The raise-and-retry pattern is currently one of the more visible demonstrations of Temporal's retry model solving a real concurrent systems problem — the workflow history shows the cadence clearly, and it makes the "Temporal handles polling for free" story concrete. With the SDK fix, this specific demonstration point softens: the activity runs quietly for minutes, and the retry story only surfaces for less-common cases like connection failures or worker crashes.

The core Temporal advantages — zero-cost crash recovery, multi-UI signaling, no custom task registry, no reconnection logic, observable workflow state — are unaffected by the SDK fix. The raise-and-retry pattern was a workaround exhibiting Temporal's strengths; those strengths exist independently of it.

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
