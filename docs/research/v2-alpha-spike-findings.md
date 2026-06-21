# Phase 0 Spike Findings — building the Tasks extension on `mcp==2.0.0a2`

Empirical results from `/tmp/spike_tasks_v2.py` (in-memory transport, no network/Temporal).
Run 2026-06-20 against `mcp==2.0.0a2`, `temporalio==1.19.0`, Python 3.13. All five unknowns resolved.

## Results

| # | Question | Result |
|---|----------|--------|
| 1 | Advertise `extensions` capability | **Server: yes** — `init_opts.capabilities.extensions = {"io.modelcontextprotocol/tasks": {}}` is assignable and passed to `server.run(...)`. |
| 1b | Client sees server extensions at `initialize` | **No** — `InitializeResult.capabilities.extensions` came back `None`. Init-time server→client advertisement does **not** round-trip in the alpha. Not a blocker: the tasks ext uses **per-request `_meta`**, which works (see #3). Follow-up only if we want init-time discovery. |
| 2a | Bare `CreateTaskResult` from `tools/call` | **Rejected** (as expected). `tools/call` result is surface-validated against `AnyCallToolResult = CallToolResult | InputRequiredResult`; a bare `{resultType:"task",...}` fails (`CallToolResult.content` required) → server `INTERNAL_ERROR`. Validation runs on **both** ends (`serialize_server_result` server-side, `validate_server_result` client-side). |
| 2b | `structuredContent` carrier from `tools/call` | **Works, no patching.** Return a valid `CallToolResult(content=[...], structuredContent=<task-envelope>)`; client reads `structuredContent` for `taskId`/`status`. |
| 2c | Monkeypatch to allow bare `resultType=="task"` | **Works.** Patching `mcp.types.methods.serialize_server_result` + `validate_server_result` to pass through `tools/call` results with `resultType=="task"` lets the bare `CreateTaskResult` round-trip. Two functions, reversible. (Runner even has a TODO acknowledging `resultType` handling is unfinished.) |
| 3 | Client raw request + per-request `_meta` | **Works.** `session._dispatcher.send_raw_request(method, params, {})`; client capability declared in `params._meta["io.modelcontextprotocol/clientCapabilities"]["extensions"]` and **readable server-side** (`params.meta`). Capability-gating is implementable. |
| 4 | `tasks/get` / `tasks/update` / `tasks/cancel` pass-through | **Works verbatim.** These methods are absent from `methods.py` spec maps, so `is_spec_method` is False and the runner skips surface validation — handlers may return arbitrary dicts. Full lifecycle confirmed: `tasks/get`→`input_required` (with `inputRequests`), `tasks/update` (empty `{}` ack), `tasks/get`→`completed` (with `result`). |
| 5 | `temporalio.plugin.SimplePlugin` | **Present** in `temporalio==1.19.0`. |

## Decisions for implementation

- **Server:** build on `mcp.server.lowlevel.Server`; register `tasks/*` via `add_request_handler(method, ParamsModel, handler)` (params model subclasses `RequestParams` so `_meta` parses). Advertise the capability by assigning `init_opts.capabilities.extensions` before `server.run`. Gate task creation on the per-request `_meta` extension declaration read from `params.meta`.
- **Client:** declare the extension per-request in `params._meta`; issue `tasks/*` over the dispatcher (raw) or via typed request models through `send_request` (KeyError on `validate_server_result` is swallowed for unknown methods, so tasks/* parse fine).
- **`tools/call` task handoff — DECIDED: patch for spec fidelity (2026-06-20).**
  Return the **bare `CreateTaskResult`** on the wire, exactly as the extension spec defines, via a
  narrow shim over the SDK's two result-validation functions. Rationale: (1) the carrier is a
  private `structuredContent` side-channel with **zero interop value** — a conformant counterpart
  couldn't recognize it; the patch produces the real wire shape. (2) With the patch, our handler/
  client code is already in final form — closing the core gap upstream only **replaces the seam**
  (see reframe below), not the domain code; the carrier instead bakes wrap/unwrap into our own code on both sides.
  (3) The patch is the *smaller* intervention (vs. subclassing `ServerRunner` / reimplementing
  `Server.run` / mutating the immutable `SERVER_RESULTS`), and fills a gap the SDK's runner `# TODO`
  already flags.
  Mitigations (required): isolate in `mcp_tasks_temporal/_sdk_compat.py` with the targeted SDK
  version noted; a **guard test** that fails loudly if the patched functions' names/signatures drift
  (so a beta bump breaks in one place, in CI); a **narrow predicate** (`method == "tools/call"` and
  `resultType == "task"` only — all other validation untouched); installed in **both** processes
  (`serialize_server_result` server-side, `validate_server_result` client-side) via a shared
  `install_tasks_result_passthrough()`.

  **Reframe (this package IS the intended native impl):** the monkeypatch is not throwaway-because-
  someone-else-will-ship-tasks — it is the *integration seam* standing in for the one core-SDK change
  "native tasks" requires. The end state isn't "delete the shim when the SDK adds tasks"; it's
  **upstream the seam** so the patch becomes a sanctioned SDK call:
  - **Option 1 — widen the result surface:** add a task variant to
    `SERVER_RESULTS[("tools/call", version)]` / `AnyCallToolResult`, gated on the declared extension.
  - **Option 2 (preferred) — result-type registration hook:** let an extension register an extra
    allowed result type for a method, so `register_tasks_extension` calls it instead of patching.
    Generalizes to any extension that augments a core method's result.
  Tracking item: file that SDK change. Until it lands, the seam stays narrow + guard-tested; the wire
  types, handlers, and client are already final. (Mirrored in `mcp_tasks_temporal/README.md` → "Path to native".)

## Caveats
- `mcp.types.methods` is documented as supported public API; `SERVER_RESULTS`/`SPEC_CLIENT_METHODS` confirm `tasks/*` are "deliberately absent."
- Init-time extension advertisement gap (#1b) is cosmetic for us — per-request `_meta` is the spec's negotiation path for tasks and works end-to-end.
