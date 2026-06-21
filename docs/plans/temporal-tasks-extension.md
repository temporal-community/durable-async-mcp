# Build a Temporal-backed MCP Tasks Extension (both edges, generic, v2 shapes)

> **Post-execution note:** after this plan was carried out, the invoice app dir was renamed
> `async_mcp/` → `invoice_processing_mcp/` and split into `server/` (server.py + invoice_backend.py)
> and `client/` (worker.py + ui.py). Phase-status prose below still uses the old `async_mcp/` paths as
> a record of what was done at the time; the run commands in **Verification** are updated to the new
> paths. The `TaskSnapshot` type was also renamed `TaskState` (method `snapshot` → `get_state`).

## Context

In the next MCP spec (2026-07-28), **Tasks graduate from a core feature to an opt-in
extension** (`io.modelcontextprotocol/tasks`, the `ext-tasks` repo). This is the unlock for
this project: instead of forking FastMCP to swap its Docket+Redis task durability for Temporal,
we can implement an **independent Temporal-backed tasks extension** and advertise it through the
standard `extensions` capability map — composing onto the SDK rather than patching its internals.

We verified, against the published alpha `mcp==2.0.0a2`, the exact ground we're building on:

- **No tasks implementation exists to consume — anywhere.** The new `mcp.types.v2026_07_28`
  module has **zero** task classes; `mcp/types/methods.py` literally comments `tasks/* deliberately
  absent`; the `ext-tasks` repo is spec-only (TypeScript schema, no code); FastMCP implements only
  the *old* SEP-1686 model. Extensions are per-SDK-maintainer discretion with no Python timeline.
  → So we are *authoring* the implementation; that is the deliverable, not a blocker.
- **The SDK gives us clean seams to do it.** `mcp.server.lowlevel.Server` **dispatches by method
  string** and exposes `add_request_handler(method, params_type, handler)` (public API). The client
  dispatcher exposes `send_raw_request(method, params, opts)`. `ClientCapabilities` /
  `ServerCapabilities` both carry an `extensions: dict[str, JSONObject]` field. So we register
  `tasks/*` handlers, issue `tasks/*` requests, and negotiate the capability — all supported surface.

**Why the client edge is the heart of this (not an afterthought):** the v2 spec puts a durability
*obligation* on the client and provides **no mechanism** — "persist task IDs durably so polling can
resume after a client crash," and it **removed `tasks/list`**, so a client that loses a task ID has
orphaned, unreachable work. That gap is the exact shape of a Temporal workflow: the workflow *is*
the durable handle, and `list_workflows` *is* the task registry the spec deleted. HITL also gets
*simpler* — the old inline `elicitation/create` push (and the whole sequential-reader concurrency
hack) is replaced by `input_required` surfaced as data in `tasks/get`, answered via `tasks/update`,
which is the same durable "await an approval signal for up to 5 days" pattern `InvoiceWorkflow`
already uses server-side. **Stateless wire, stateful edges — Temporal on both edges.**

**Decisions (confirmed):** official SDK v2 low-level (drop standalone `fastmcp`); build now on the
`2.0.0a` alpha; in-place rewrite on the `temporal-client` branch; **both edges**, as **one generic,
reusable extension**, against the **new v2 extension shapes** (hand-defined Pydantic types).

## Architecture

A new reusable package `mcp_tasks_temporal/` implements the extension generically; the invoice app
in `async_mcp/` becomes its first *consumer* (plugging in invoice-specific behavior).

```
mcp_tasks_temporal/
  wire.py        Hand-defined Pydantic types for the v2 tasks extension (see shapes below)
  _sdk_compat.py install_tasks_result_passthrough(): the integration seam over the SDK's
                 serialize_server_result (server) / validate_server_result (client) so a bare
                 CreateTaskResult (resultType=="task") passes tools/call validation on both ends.
                 Guard-tested; resolves into a sanctioned SDK call once the result-surface gap is
                 closed upstream — the one core change "native tasks" needs. (See spike findings /
                 README "Path to native".)
  server.py      register_tasks_extension(server, backend): installs the compat shim, wires
                 tasks/* handlers + a CreateTaskResult-returning tools/call onto a lowlevel Server;
                 advertises the capability. Calls into a TaskBackend protocol for app behavior.
  backend.py     TaskBackend protocol + TemporalTaskBackend (task ID = workflow ID; status query,
                 input-request build, input-response apply via signals, terminal result, cancel).
  client/
    activities.py  Generic MCP wire activities: start_task, poll_task (returns status +
                   inputRequests), submit_task_input (tasks/update), get_task_result.
    workflows.py   Generic TaskTrackerWorkflow: durable poll loop; on input_required stores the
                   inputRequests (queryable by UI) and awaits a user_decision signal; submits via
                   tasks/update; completes with the terminal result.
    session.py     Owns the v2 mcp.client session; declares the tasks extension in per-request _meta.
  plugin.py      MCPTasksClientPlugin — the Temporal Plugin that PACKAGES the client edge.
```

### Packaging as a Temporal Plugin (the install mechanism)
Two distinct senses of "extension" compose here: the **MCP extension** (`io.modelcontextprotocol/tasks`,
the protocol contract) is *implemented by* this package and *distributed as* a **Temporal Plugin**
(`temporalio.plugin`, the SDK's reusable-bundle mechanism). One-liner: *we ship the durable MCP-tasks
client as a Temporal Plugin — one registration call gives any task-speaking MCP server a
crash-resilient, `tasks/list`-free durable client.*

- **Client edge (primary fit):** `plugin.py` builds an `MCPTasksClientPlugin` (via `SimplePlugin`,
  or a `Plugin` subclass if we need `configure_worker`'s run/lifecycle hook to own the MCP session
  for the worker's lifetime). It bundles `workflows=[TaskTrackerWorkflow]` and the wire activities,
  and sets up the shared `mcp.client` session. Consumers adopt it with one call:
  `Worker(client, task_queue=..., plugins=[MCPTasksClientPlugin(mcp_config)])` — no manual
  workflow/activity registration.
- **Server edge (lighter fit):** the server's primary surface is MCP-side
  (`register_tasks_extension` on the lowlevel `Server`), not a Temporal worker. Temporal is used
  there only as a *client* to drive the business workflow. So provide an optional **client** Plugin
  bundling the Temporal client config / data converter the `TemporalTaskBackend` uses; do NOT force
  the MCP handler registration through the Temporal Plugin abstraction (wrong layer).

### Generic seams (what the app plugs in)
- **Server `TaskBackend`** (invoice impl): start the job → return task ID (start Temporal workflow,
  ID = task ID); map job state → MCP task status (`TEMPORAL_TO_MCP_STATE`); build `inputRequests`
  when `input_required` (approve/reject elicitation form from `GetInvoiceData`); apply
  `inputResponses` (signal `ApproveInvoice`/`RejectInvoice`); build terminal `result`; cancel.
- **Client** is genuinely app-agnostic: it polls any task, surfaces *any* `inputRequests` schema,
  awaits a generic decision, submits *any* `inputResponses`. The invoice UI is just one renderer of
  an arbitrary `requestedSchema`.

## Wire shapes to define in `wire.py` (from the draft ext-tasks spec)

```
CreateTaskResult  : resultType:"task", { taskId, status, statusMessage?, createdAt,
                    lastUpdatedAt, ttlMs|null, pollIntervalMs? }   ← returned from tools/call
status ∈ { working, input_required, completed, failed, cancelled }  (last 3 terminal)
GetTaskResult     : resultType:"task" + DetailedTask variant:
                    input_required → inputRequests map; completed → result; failed → error
inputRequests     : { "<key>": { method:"elicitation/create",
                                  params:{ mode, message, requestedSchema } } }
UpdateTaskRequest : params { taskId, inputResponses:{ "<key>": { action, content? } } } → empty ack
CancelTaskRequest : params { taskId } → empty ack (cooperative)
Capability        : client per-request _meta.io.modelcontextprotocol/clientCapabilities
                                          .extensions["io.modelcontextprotocol/tasks"] = {}
                    server ServerCapabilities.extensions["io.modelcontextprotocol/tasks"] = {}
```
Input-request keys MUST be unique over a task's lifetime; the invoice approval gate is one-shot, so
a fixed key (`"approval"`) is fine — note this constraint for multi-gate consumers.

## Implementation phases

### Phase 0 — Spike (resolve the last SDK details against the alpha)
A throwaway script: lowlevel `Server` + `mcp.client` session over stdio. Confirm (1) how to
**advertise `extensions`** in `ServerCapabilities` (constructor/init options vs. the capabilities-
derivation in `lowlevel/server.py`), (2) that a registered `tools/call` handler can **return a
non-standard result** (our `CreateTaskResult` dict), (3) the exact **client raw-request** signature
for issuing `tasks/get` / `tasks/update` and setting `_meta`, (4) that `add_request_handler` accepts
arbitrary extension method strings, (5) that the installed `temporalio` exposes
`temporalio.plugin.SimplePlugin` and a `configure_worker` run/lifecycle hook capable of owning the
MCP session for the worker's lifetime (else use a `Plugin` subclass / bump the pin). Deliverable: a
trivial task round-trips create → get → update → get(completed).

### Phase 1 — `mcp_tasks_temporal/` core (TDD)
- `wire.py` — the Pydantic types above. Unit-test serialize/parse against literal spec JSON.
- `server.py` — `register_tasks_extension(server, backend)`: `add_request_handler` for `tasks/get`,
  `tasks/update`, `tasks/cancel`; a `tools/call` wrapper that checks the client declared the
  extension in `_meta` (MUST NOT create a task otherwise), starts the job via `backend`, returns
  `CreateTaskResult`; advertise the capability. Test handlers against a fake in-memory backend.
- `backend.py` — `TaskBackend` protocol + `TaskState` dataclass (the generic contract the
  tasks/* handlers call into). The concrete Temporal-backed implementation is **deferred to Phase 3**
  (`InvoiceTaskBackend`): its substance — status mapping, signal names, elicitation schema — is
  app-specific and only testable against the real workflows. Durability of "task created before
  response" is satisfied because `start_workflow` returns only after the workflow is persisted.
  *(Status: Phase 1 done — 16 tests green: wire round-trip, compat-shim guard, server lifecycle
  end-to-end over the in-memory transport with a fake backend.)*

### Phase 2 — generic client (`mcp_tasks_temporal/client/`, TDD) — DONE (7 new tests, 23 total green)
- `activities.py` — `start_task`, `poll_task` (→ status + optional `inputRequests` + `result`),
  `submit_task_input` (tasks/update), `cancel_task`. **Result retrieval folded into `poll_task`**:
  in v2 the result rides in the terminal `tasks/get`, so a separate `get_task_result` activity is
  redundant — dropped. All the obsolete old-client machinery (`_elicitation_handler`,
  `_active_elicitations`, `x-task-id` smuggling, the background get-result + cancel dance, the
  20ms-raise workaround, `maximum_interval` cap) simply never exists here — elicitation is
  pull-based data. `models.py` holds sandbox-safe dataclasses + activity-name string constants
  (workflow references activities by name, so it imports no `mcp`). `session.py` owns the stdio
  session + installs the client-side compat shim + injects capability `_meta` per request.
- `workflows.py` — generic `TaskTrackerWorkflow`: durable poll loop respecting `pollIntervalMs`; on
  `input_required` store `inputRequests` (query `get_pending_input` for the UI) and await
  `user_decision` signal; call `submit_task_input`; loop to terminal; return result. Keep
  `start_time_skipping()` tests + sandbox passthrough (drop `fastmcp`, keep `mcp`, `beartype`).
- `session.py` — own the v2 `mcp.client` stdio session; declare the extension capability per request.
- `plugin.py` — `MCPTasksClientPlugin` packaging the above as a Temporal Plugin
  (`workflows=[TaskTrackerWorkflow]` + wire activities + MCP-session setup). Test that a
  `Worker(..., plugins=[MCPTasksClientPlugin(...)])` registers the workflow/activities without
  manual wiring.

### Phase 3 — rewire the invoice app as the consumer — DONE (8 new tests; 33 total green; venv cut over)
Status: `async_mcp/invoice_backend.py` (`InvoiceTaskBackend`, the concrete Temporal backend) +
`async_mcp/server.py` (lowlevel Server + `register_tasks_extension`, in-memory-tested end-to-end);
`temporal_task_handlers.py` and the old `client_worker/{workflows,activities,models}.py` deleted;
`client_worker/worker.py` is now a one-call plugin adopter (worker-registration smoke-tested);
`ui.py` renders generic `inputRequests`; legacy `mcp_client/` quarantined; `pyproject.toml` cut over
to `mcp[cli]==2.0.0a2` (+`temporalio>=1.19`, `click`) and the venv recreated (no `fastmcp`). All
rewritten entrypoints import cleanly. **Remaining for Phase 4:** the three old `async_mcp/tests`
(handlers/activities/tracker) still import deleted modules — delete/rewrite; then docs.

Original detail:
- `async_mcp/server.py` — build on `mcp.server.lowlevel.Server`; define `process_invoice` and call
  `register_tasks_extension(server, InvoiceTaskBackend(temporal_client))`. Delete
  `temporal_task_handlers.py` (its logic moves into the generic extension + the invoice backend).
- `async_mcp/client_worker/` — `worker.py` becomes a one-call adopter:
  `Worker(client, task_queue=..., plugins=[MCPTasksClientPlugin(mcp_config)])` (no manual
  workflow/activity registration); `ui.py` (Temporal-only) renders the generic
  `inputRequests`/decision (approve/reject is one schema it knows how to draw); `models.py`
  generalized (carry the input-request key + arbitrary schema).
- Decide the legacy `async_mcp/mcp_client/` (OpenAI, depends on `fastmcp.Client` + old task API):
  **recommend quarantine/retire** — superseded, nothing imports it, and it blocks dropping fastmcp.

### Phase 4 — deps, tests, docs — DONE (32 tests green; black/isort/flake8 clean)
Status: deleted the 4 superseded old tests; `testpaths` → `["mcp_tasks_temporal/tests", "async_mcp/tests"]`;
added `.flake8` (black-aligned, excludes retired `mcp_client`/unmigrated `durable_sync_mcp`); rewrote
CLAUDE.md (overview, structure, commands, architecture, key patterns), README, and the research-doc
TODO (→ done); refreshed memories. **Out-of-scope discovery:** `durable_sync_mcp/` is FastMCP
(`mcp<2`) and can't coexist with the v2 venv — left unmigrated and documented (raise with user).

Original detail:
- `pyproject.toml` — replace `fastmcp[tasks]>=2.14.0` with `mcp[cli]==2.0.0a2` (pinned; unpinned
  resolves to v1). Bump `temporalio` to a version exposing `temporalio.plugin.SimplePlugin` if the
  current pin predates it. Update sandbox passthrough modules.
- Rewrite `async_mcp/tests/` for the new shapes: `CreateTaskResult` from `tools/call`; `tasks/get`
  carries `inputRequests` when `input_required`; `tasks/update` signals the workflow; terminal
  `tasks/get` carries `result`. Drop `tasks/result` and `tasks/list` tests. Add `mcp_tasks_temporal`
  unit tests (wire round-trip, server handlers vs. fake backend, client workflow poll→input→update).
- Update `CLAUDE.md` (new package, lifecycle diagram, dropped concurrency-hack writeup) and
  `docs/research/mcp-2026-07-28-spec-impact.md` (TODO → done; record the SDK-omits-tasks finding).

## Critical files
- New: `mcp_tasks_temporal/{wire,server,backend}.py`, `mcp_tasks_temporal/client/{activities,workflows,session}.py`, `mcp_tasks_temporal/plugin.py`
- Rewrite: `async_mcp/server.py`; `async_mcp/client_worker/{worker,ui,models}.py`
- Delete: `async_mcp/temporal_task_handlers.py` (logic absorbed into the extension + invoice backend)
- `pyproject.toml`, `async_mcp/tests/*`

## Verification (end-to-end)
1. Phase-0 spike script round-trips a task on the alpha.
2. `uv run pytest` green (wire, server-handlers, client-workflow, invoice integration).
3. Manual demo: Temporal dev server → `python -m bizservice.worker` →
   `python -m invoice_processing_mcp.client.worker` (spawns the MCP server over stdio) →
   `python -m invoice_processing_mcp.client.ui`. Submit an invoice → `tools/call` returns
   `CreateTaskResult` → poll shows `working` → `input_required` → UI surfaces the approval
   elicitation → approve → `tasks/update` signals the workflow → `completed`, result `PAID`.
4. **Durability proof (the thesis):** kill the client worker mid-task; restart it; the
   `TaskTrackerWorkflow` resumes polling from durable state with the same task ID (no `tasks/list`
   needed). Then kill the server worker mid-task; the Temporal `InvoiceWorkflow` survives and the
   client's next `tasks/get` re-binds. Both edges durable across crashes.
