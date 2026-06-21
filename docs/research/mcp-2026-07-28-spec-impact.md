# Impact of the MCP 2026-07-28 Spec on This Project

Research notes from reviewing the MCP 2026-07-28 release candidate against the
current implementation. Written 2026-05-31.

## Sources

- [The 2026-07-28 MCP Specification Release Candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/) — canonical announcement
- [MCP Tasks extension overview](https://modelcontextprotocol.io/extensions/tasks/overview) — authoritative description of the redesigned Tasks lifecycle
- [`experimental-ext-tasks` repo](https://github.com/modelcontextprotocol/experimental-ext-tasks) — full Tasks extension spec (SEP-2663)
- [MCP is Growing Up](https://aaif.io/blog/mcp-is-growing-up/) — third-party summary that prompted this review
- Cross-referenced against this project's own [`tasks-protocol-gaps.md`](tasks-protocol-gaps.md) and [`mcp-tasks-limitations-research.md`](mcp-tasks-limitations-research.md)

## Status of the RC and SDKs (as of 2026-05-31)

- **Spec: release candidate.** The RC locked 2026-05-21; the final spec ships 2026-07-28. It contains **breaking changes**.
- **SDKs: not yet updated.** The latest `modelcontextprotocol/python-sdk` releases still target the `2025-11-25` spec. There is no release implementing the stateless protocol or the Tasks-as-extension lifecycle yet. The concurrent-dispatch PR ([python-sdk#2490](https://github.com/modelcontextprotocol/python-sdk/pull/2490)) we had been tracking was still open on this date — and is now largely moot for us (see §1).
- **Caveat on field names.** Names below (`CreateTaskResult`, `resultType: "task"`, `inputRequests`, `inputResponses`, `ttlMs`, `pollIntervalMs`) come from the extension overview page; confirm exact shapes against the `experimental-ext-tasks` repo before writing code against them.

This project currently targets the **2025-11-25 experimental** Tasks spec. The RC explicitly states: *"Anyone who shipped against the `2025-11-25` experimental Tasks API will need to migrate to the new lifecycle."* That's us. Migration is a future effort gated on SDK availability (see TODO at the end).

## The three changes that matter here

### 1. The redesigned Tasks lifecycle removes both blocking and server-initiated elicitation (largest impact)

Tasks graduated from an experimental core feature (SEP-1686, baked into 2025-11-25) to an
**official extension** (SEP-2663, ID `io.modelcontextprotocol/tasks`, in the
[`experimental-ext-tasks`](https://github.com/modelcontextprotocol/experimental-ext-tasks)
repo). The lifecycle was redesigned around the stateless model, and two of those changes hit
the heart of this implementation.

**`tasks/result` is gone; retrieval is pure polling.** The methods are now `tasks/get`,
`tasks/update`, `tasks/cancel`. The final result rides in the `tasks/get` response once the
task is terminal — `result` on `completed`, `error` on `failed`. There is **no blocking method
left**. The extension's own "Why not just block?" section spells out the rationale we'd
documented independently: no long-lived connections, crash resilience via a durable handle,
progress visibility, mid-flight interaction without unsolicited messages.

**Elicitation moves into the task, answered via `tasks/update` — no server-initiated push.**
When a task reaches `input_required`, the `tasks/get` response carries an `inputRequests` map
(elicitations, etc.); the client fulfills them by calling `tasks/update` with `inputResponses`.
The spec states it directly: *"no second connection or unsolicited server-to-client messages
required."*

**Why it's the largest impact:** the entire Gap 3 problem — the sequential-reader bottleneck
and the check-once-and-raise-within-~20ms workaround in
`async_mcp/client_worker/activities.py` (`_elicitation_handler`) — exists *only* because the
old model pushed `elicitation/create` inline on a shared receive loop. The new model has no
unsolicited server→client request at all: the client *polls* `tasks/get`, sees pending
`inputRequests`, and responds via `tasks/update`. Consequences:

- **Gap 3 (sequential-reader blocking) is eliminated, not merely mitigated.** There is no
  inline server→client request to starve the reader. python-sdk#2490 (opt-in concurrent
  dispatch) is no longer needed for our case.
- **Gap 2 (blocking `tasks/result`) is eliminated.** The method that "MUST block until
  terminal" no longer exists. Our non-compliant fast-fail divergence is moot — everyone polls now.
- **This is a breaking rewrite, not a tweak.** `handle_tasks_result` in
  `temporal_task_handlers.py` (which calls `elicit_form` and blocks on `handle.result()`) is
  replaced by serving `tasks/get` (carry status + `inputRequests`, then `result`/`error` when
  terminal) and `tasks/update` (apply `inputResponses` → signal the workflow). The client
  `_elicitation_handler` disappears; the `TaskTrackerWorkflow` polls and submits via
  `tasks/update`.
- **The `x-task-id` routing hack disappears.** There's no out-of-band elicitation callback to
  correlate — input requests arrive *attached to the task* the client is already polling.

**Earlier framing corrected:** the prior version of this doc attributed the elicitation change
to the core-protocol "Multi Round-Trip Requests / `InputRequiredResult`" mechanism (SEP-2322).
Right direction, wrong layer — the Tasks extension handles it through `input_required` +
`tasks/get` `inputRequests` + `tasks/update`, not a core `InputRequiredResult` response.

**Other lifecycle details:**
- A `tools/call` returns either the normal result or a `CreateTaskResult` tagged
  `resultType: "task"` (carrying `taskId`, initial status, `ttlMs`, `pollIntervalMs`). The task
  is durably created before the response is sent.
- Status values: `working`, `input_required`, `completed`, `failed`, `cancelled` (the last three
  terminal). Cancellation via `tasks/cancel` is **cooperative** — acknowledged, not guaranteed —
  which maps cleanly onto Temporal's cancel semantics.
- Server-directed and capability-gated: client opts in once via `io.modelcontextprotocol/tasks`
  in per-request `_meta` capabilities; server advertises in `server/discover` and decides
  per-request whether to materialize a task. A server **must not** return a task to a client
  that didn't declare support.
- Optional `notifications/tasks/status` (opt-in via `subscriptions/listen`) carries full task
  state; **polling is the default**.

**Architectural validation for the talk:** the extension's "External job systems" guidance —
*"return a task when you create the job and resolve it when the job completes"* — is literally
the workflow-ID = task-ID pattern. The protocol moved *toward* the durable-execution
architecture.

### 2. Stateless protocol layer (validating)

The `initialize`/`initialized` handshake and `Mcp-Session-Id` header are removed; client info,
protocol version, and capabilities now travel in `_meta` on every request. The replacement
model — servers returning explicit handles the client reasons about and logs as workflow
artifacts — restates our workflow ID = task ID thesis almost verbatim.

- **Our Gap 2 divergence is now spec-aligned.** Raising `McpError` on `working` instead of
  holding a connection open for up to five days is no longer non-compliant-but-pragmatic; the
  new Tasks lifecycle removes the blocking method entirely and makes polling the only path (§1).
- **Transport-level rework risk.** `temporal_task_handlers.py` overwrites five request handlers
  on the low-level SDK server. If the SDK reworks request handling for statelessness (no
  handshake, `_meta`-carried capabilities), that override surface shifts. This is the most
  likely place to break on an SDK upgrade.
- **Not the same as `stateless_http=True`.** The pre-existing FastMCP/SDK
  `stateless_http`/`json_response` options concern the *HTTP transport*, not the new
  protocol-level stateless model. Don't conflate them when migrating.

### 3. `tasks/list` is removed (cleanup)

> tasks/list is removed because it can't be scoped safely without sessions.

- `handle_tasks_list` in `temporal_task_handlers.py` becomes dead / non-compliant code.
- **All of Gap 1** in [`tasks-protocol-gaps.md`](tasks-protocol-gaps.md) (no tool association,
  no filtering on `tasks/list`) becomes moot — the endpoint it analyzed is gone.
- **No harm to us.** Our recovery path never depended on `tasks/list`: the UI rediscovers
  in-flight work via Temporal's `list_workflows`, and task IDs live in workflow state. The
  spec removed the fragile primitive we had already routed around.

## Lower-impact changes

| Change | Impact here |
|---|---|
| **Sampling deprecated** (use direct LLM provider integration) | Validates the legacy `mcp_client` using the OpenAI Responses API directly. No change. |
| **Logging deprecated** (stderr / OpenTelemetry) | Orthogonal — Temporal observability is separate. No change. |
| **Roots deprecated** | Not used. No change. |
| **Server-directed task creation** ("client advertises the extension, server decides") | Matches `TaskConfig(mode="required")`, but the negotiation changes: client opts in per-request via `io.modelcontextprotocol/tasks` capability; server must not return a task to a non-declaring client. FastMCP API change. |
| **Extensions framework** (reverse-DNS IDs, `ext-*` repos, independent versioning) | Tasks is now an extension to advertise/negotiate rather than an experimental core feature. Affects how we declare task capability once the SDK exposes it. |
| **Deprecation policy** | Deprecated methods keep working for ≥12 months, so the 2025-11-25 implementation won't break overnight — but the stateless/multi-round-trip changes are *breaking*, not deprecations, and have no such grace window. |

## Bottom line

Net positive for the **thesis**, net work for the **code**. The protocol moved toward durable,
explicitly-handled, stateless interactions — the exact point the Temporal integration makes. But
our headline elicitation workaround (the most visible "Temporal solves concurrency for free" demo
moment) now solves a problem the spec is removing. The durable-execution value we actually want to
showcase — crash recovery, multi-UI signaling, no task registry, server-side resumption by any
instance — survives intact and arguably gets *easier* to tell against a stateless protocol.

For the repositioned thesis argument (why durable execution is the substrate the stateless
protocol's edge semantics presuppose — "stateless wire, stateful edges"), see
[`../design/durable-client-thesis-after-stateless-redesign.md`](../design/durable-client-thesis-after-stateless-redesign.md).

## DONE (2026-06-20): migrated to the 2026-07-28 Tasks extension

**Outcome:** rather than wait for SDK support, we *authored* the Tasks extension as a generic,
Temporal-backed implementation in `mcp_tasks_temporal/` (distributed as a Temporal Plugin), with the
invoice app as its consumer. Built against `mcp==2.0.0a2`. The SDK alpha **deliberately omits** the
tasks extension (it ships only the `extensions` capability container), so the wire types and `tasks/*`
handlers are hand-written; `tools/call` returning a bare `CreateTaskResult` needs a narrow shim
(`_sdk_compat`) — see the empirical spike in [`v2-alpha-spike-findings.md`](v2-alpha-spike-findings.md)
and the plan in [`../plans/temporal-tasks-extension.md`](../plans/temporal-tasks-extension.md).

Net result: the blocking `tasks/result`, server-initiated elicitation, the sequential-reader
concurrency workaround, and `tasks/list` are all gone; HITL is pull-based (`input_required` →
`tasks/get` `inputRequests` → `tasks/update`). The original migration checklist (now complete),
for reference:

1. **Confirm shapes** (`CreateTaskResult`, `resultType: "task"`, `inputRequests`,
   `inputResponses`, status values, `ttlMs`, `pollIntervalMs`) against the released
   `experimental-ext-tasks` spec and SDK types.
2. **Rewrite the server task handlers.** Replace the blocking `handle_tasks_result`
   (`elicit_form` + block-on-`handle.result()`) with:
   - `tasks/get` → query workflow status, return `result`/`error` when terminal, and surface an
     `inputRequests` map while in `input_required`.
   - `tasks/update` → apply `inputResponses` by signaling the workflow (`ApproveInvoice` /
     `RejectInvoice`).
   Return a `CreateTaskResult` (`resultType: "task"`) from the wrapped `tools/call`.
3. **Rewrite the client side.** Delete `_elicitation_handler` and the `x-task-id` smuggling;
   `TaskTrackerWorkflow` polls `tasks/get` and submits decisions via `tasks/update`. There is no
   server-initiated elicitation callback to register anymore.
4. **Delete the Gap 3 workaround.** Remove the 20ms raise-and-retry pattern and the
   `maximum_interval` retry cap in `client_worker/activities.py`; the sequential-reader
   bottleneck no longer exists. Drop the dependency on python-sdk#2490.
5. **Remove `handle_tasks_list`** (endpoint gone) and update CLAUDE.md / design docs.
6. **Audit the overwritten request handlers** in `temporal_task_handlers.py` against the
   stateless request-handling model (no `initialize` handshake, capabilities in `_meta`) and the
   move of Tasks from core to an extension — the override surface will shift.
7. **Update capability negotiation** to the `io.modelcontextprotocol/tasks` extension
   (per-request client capability in `_meta`; server advertised in `server/discover`) once
   FastMCP exposes it.
8. **Revise the affected research/design docs:** Gap 1 and Gap 2 in
   [`tasks-protocol-gaps.md`](tasks-protocol-gaps.md) are now resolved/moot; Gap 3's workaround
   becomes historical context.
