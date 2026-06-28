# Plan: Swap the MCP Tasks protocol from v2 → v1, keeping all v2 features

## Context

This branch (`temporal-client-v1mcp`) forked from `temporal-client-mcpv1` and added the **v2** MCP
Tasks work on top: a pull-based protocol (`tasks/get` / `tasks/update`), a reusable
`mcp_tasks_temporal/` package, a lowlevel (FastMCP-less) server, plus net-new orchestration value —
`PurchaseOrderWorkflow`, the NiceGUI board, the cost-center 2nd HITL gate, and back-office activities.

The goal is to **keep the entire v2 implementation and its features, but switch the MCP tasks
*protocol* underneath from v2 (pull) back to v1 (FastMCP + server-push elicitation)** — i.e. "go back
to FastMCP and my workflow logic" while preserving everything the current branch gained. The deleted
OpenAI/LLM CLI client stays deleted (no `openai` dependency).

The v1 reference source lives only on branch `temporal-client-mcpv1`, under `async_mcp/` — read it with
`git show temporal-client-mcpv1:<path>`. (Note the confusingly-named branches: current is
`temporal-client-v1mcp`; the v1 source is on `temporal-client-mcpv1`.)

### Core design (validated against both implementations)

Freeze **two contracts** so the high-level features barely change, and swap only what's between them:

1. **Client-side `TaskTrackerWorkflow` external API stays at v2's** — `TaskTrackerInput(tool_name,
   arguments)`, dict return `{status, result, error}`, queries `get_status` / `get_pending_input`
   (v2 keyed shape) / `get_task_id`, signal `user_decision(decision: dict)`. The *internals* become
   v1's: on `input_required`, run a `handle_elicitation` activity that triggers the server push,
   instead of calling `tasks/update`. Result: `PurchaseOrderWorkflow`, `ui.py`, `gui.py` are
   essentially untouched.
2. **Server-side `InvoiceTaskBackend` domain logic stays** — `TEMPORAL_TO_MCP_STATE`,
   `_approval_request`, `_cost_center_request`, the keyed `input_requests` in `get_state`, and the
   signal routing in `submit_input`. A new generic FastMCP `tasks/result` handler reuses them:
   `get_state` → if `input_required`, `elicit_form(message, schema)` → `submit_input(...)`.

What genuinely changes: the transport/session (stdio `ClientSession` → FastMCP `Client` with an
`elicitation_handler`), the wire calls (`tasks/get`+`tasks/update` → `call_tool(task=True)` +
`get_task_status` + push elicitation during `tasks/result`), the server (lowlevel `Server` +
`register_tasks_extension` → `FastMCP` + ported task handlers), and the dependency
(`mcp==2.0.0a2` → `fastmcp[tasks]>=2.14.0`).

### Three corrections that are load-bearing
- v1's `TaskTrackerWorkflow` has **no `get_status`** — the GUI is built on it. It must be **added**
  (track `self._status` in the poll loop).
- v1's `get_elicitation_details` returns a flat `ElicitationDetails(message, schema)`; the v2 UIs
  iterate the **keyed** shape `{key: {"method": ..., "params": {"message", "requestedSchema"}}}`.
  The ported `_elicitation_handler` must synthesize that keyed shape and signal it so
  `get_pending_input` matches the UIs (fails at runtime in the UI, not at import — highest risk).
- v1 answers a single hardcoded `{"value": decision}`. For multi-field (cost-center
  `{cost_center, memo}`) and multi-gate routing, `user_decision` becomes a `dict`, and the server
  must inject **both `x-task-id` and `x-request-key`** into `requestedSchema` so the handler selects
  `decision[x-request-key]` and returns `ElicitResult(action=..., content=...)` from it.

### Reintroduced caveat (call out, don't try to fix)
Server-push elicitation over one shared stdio session brings back the **one-shot-raise
reader-release** workaround: `_elicitation_handler` raises immediately if no decision is ready so
Temporal retries with backoff, releasing the MCP reader between attempts. Per
`docs/research/tasks-protocol-gaps.md` this is an MCP Python SDK reader-loop gap (SEP-2322 proposed) —
**no merged SDK PR removes it**. Port the `RetryPolicy(maximum_attempts=0, maximum_interval=10s)` and
the post-elicitation `workflow.sleep(2s)` as-is.

---

## Change list (by layer)

### Layer 0 — Dependencies
- **`pyproject.toml`**: replace the `mcp==2.0.0a2` pin with `fastmcp[tasks]>=2.14.0`; keep
  `temporalio>=1.19` (SimplePlugin floor — do **not** drop to v1's `>=1.0.0`), `nicegui`, `click`. Do
  **not** add `openai`. Update the comment explaining the old pin. Regenerate `uv.lock`.

### Layer 1 — `mcp_tasks_temporal/` server side
- **Delete** `wire.py` and `_sdk_compat.py` (v2-only, dead under FastMCP).
- **`backend.py`**: keep `TaskBackend` Protocol + `TaskState`; drop the `wire` imports; redefine
  `InputRequest`/`InputResponse` as local plain dataclasses / `dict` aliases (no longer wire types).
  Add one method to the Protocol: `wait_result(task_id) -> dict` (the terminal `CallToolResult`).
- **`server.py`**: rewrite `register_tasks_extension(mcp: FastMCP, backend, *, task_tools)` by
  generalizing v1's `temporal_task_handlers.py` onto the `backend` seam (instead of hardwired
  `InvoiceWorkflow`):
  - wrapped `call_tool` for task tools → `backend.start(name, args)` → `CallToolResult` carrying
    `_meta` task id + status.
  - `tasks/get` → `backend.get_state` → status-only `GetTaskResult` (no input_requests on the wire).
  - `tasks/result` → `backend.get_state`; if terminal → result/error; if `input_required` → pick the
    single pending `input_requests` entry, inject `x-task-id` + `x-request-key` into its
    `requestedSchema`, `await ctx.session.elicit_form(message, schema)`, `backend.submit_input(task_id,
    {key: {action, content}})`, then `await backend.wait_result(task_id)` (abandoned on client cancel).
  - `tasks/cancel` → `backend.cancel`. `tasks/list` optional (the client never calls it; skip).

### Layer 2 — `mcp_tasks_temporal/` client side
- **`client/models.py`**: keep `TaskTrackerInput(tool_name, arguments)`, `TASK_QUEUE`,
  `TERMINAL_STATUSES`; add activity-name constants `START_TASK`, `POLL_TASK`, `HANDLE_ELICITATION`,
  `GET_TASK_RESULT`; drop the v2-only `SubmitInput`/`TaskPollResult`/submit/cancel names. Stay mcp-free.
- **`client/workflows.py`**: port v1's poll/elicitation loop but keep the **v2 external surface** —
  `TaskTrackerInput(tool_name, arguments)`; track and expose `get_status`; expose `get_pending_input`
  in the keyed shape; `user_decision(decision: dict)`; keep `get_task_id`; dict return. Reference
  activities by **string** (`models.HANDLE_ELICITATION`, etc.) — do **not** import `MCPActivities`,
  so the workflow sandbox stays mcp-free. Keep the indefinite `RetryPolicy` + 2s sleep + reset.
- **`client/activities.py`**: port v1 `MCPActivities` (holds a `fastmcp.Client`,
  `_active_elicitations`, `_elicitation_events`, bound `_elicitation_handler`, `start_task`,
  `poll_task_status`, `handle_elicitation`, `get_task_result`). Generalize: `start_task` uses
  `call_tool(tool_name, arguments, task=True)` (not hardcoded `process_invoice`); `_elicitation_handler`
  reads `x-task-id` + `x-request-key`, builds the keyed shape, signals `elicitation_received`, reads
  `get_pending_decision` (now a dict), returns `ElicitResult` from the matching entry or raises;
  decorate with `@activity.defn(name=models.*)` to match the workflow's string refs. Keep a
  `bind(session)` hook.
- **`client/session.py`**: replace stdio `ClientSession` + raw `task_request` with an
  `@asynccontextmanager connect_tasks_session(config, elicitation_handler)` that does
  `async with FastMCPClient(config, elicitation_handler=...) as mcp: yield mcp`. Takes the
  **Claude-Desktop config dict** (FastMCP's input), not `StdioServerParameters`.
- **`plugin.py`**: keep `MCPTasksClientPlugin` as a `SimplePlugin`. In `run_context`: build
  `MCPActivities(mcp=None, temporal_client=<plugin client>)`, open
  `FastMCPClient(config, elicitation_handler=acts._elicitation_handler)`, set `acts._mcp = mcp`, yield.
  Signature becomes `MCPTasksClientPlugin(config, ...)`. (If the plugin context doesn't expose the
  worker's Temporal client to `run_context`, accept the address/client as a plugin arg.)

### Layer 3 — Invoice server (`invoice_processing_mcp/server/`)
- **`server.py`**: build `FastMCP("invoice_processor")` with `@mcp.tool(task=TaskConfig(mode="required"))
  process_invoice(invoice: Invoice)` (port v1's Pydantic `Invoice`/`LineItem`, replacing the
  hand-written JSON schema). Call `register_tasks_extension(mcp, InvoiceTaskBackend(client),
  task_tools={INVOICE_TOOL})`; `main()` → `mcp.run(transport="stdio")`. `backend.start` is
  authoritative (the wrapper intercepts the tool call).
- **`invoice_backend.py`**: keep almost everything — `TEMPORAL_TO_MCP_STATE` (incl. `RECONCILING`,
  `PENDING-COST-CENTER`, `CODED`, `PAYING`), `_approval_request`, `_cost_center_request`, both-gate
  `get_state`, and `submit_input` routing. Drop the `wire` imports; add `wait_result(task_id)` =
  `await handle.result()` → `_terminal_result(...)`. `submit_input` already tolerates the
  `{key: {action, content}}` shape.
- **`server/__main__.py`**: update to call the new `main()` (likely unchanged).

### Layer 4 — Invoice client (`invoice_processing_mcp/client/`)
- **`purchase_order_workflow.py`**, **`ui.py`**, **`gui.py`**, **`backoffice_activities.py`**,
  **`client_config.json`**, **`__main__.py`**: **no change** (Contract 1 preserves their query/signal
  surface; `gui.build_responses` already produces `{key: {action, content}}`).
- **`worker.py`**: `MCPTasksClientPlugin(server_params)` → `MCPTasksClientPlugin(config)`; replace the
  `_server_params` `StdioServerParameters` helper with loading the config dict. Keep
  `with_passthrough_modules("beartype")` only (do **not** add `fastmcp`/`mcp` — a need for that signals
  a sandbox leak to fix). Keep `PurchaseOrderWorkflow` + back-office activities listed.

### Layer 5 — Tests
- **Delete** `mcp_tasks_temporal/tests/test_wire.py`, `test_sdk_compat.py`.
- **`test_server.py`**: re-port to v1-style handler tests (mirror v1 `test_task_handlers.py`) over
  `WorkflowEnvironment.start_time_skipping()` + a real `InvoiceWorkflow`; **extend** to the two-gate
  large-invoice path asserting `tasks/result` elicits twice (approve enum, then `{cost_center, memo}`)
  with `x-task-id`/`x-request-key` injected.
- **`test_client_workflow.py`**: port v1 `test_task_tracker_workflow.py`; **extend** to assert
  `get_status` transitions, the keyed `get_pending_input` shape, `user_decision(dict)` round-trip, and
  a two-round elicitation sequence.
- **`test_client_activities.py`**: port v1 `test_mcp_activities.py`; **extend** the `_elicitation_handler`
  to route by `x-request-key` (approval + cost-center, accept/decline), the one-shot-raise, and a
  generic `start_task` over `tool_name`.
- **`test_plugin.py`**: assert the plugin registers `TaskTrackerWorkflow` + the four activities and that
  `run_context` builds + binds a FastMCP client (mock `FastMCPClient`).
- **`invoice_processing_mcp/tests/test_invoice_backend.py`**: rewrite to test the backend directly
  against a Temporal handle (drop the v2 in-memory `ClientSession`/`client_capability_meta`); cover both
  gates' `get_state` and both `submit_input` routings.
- **`test_purchase_order_workflow.py`**: update `FakeMCPActivities` to the v1 activity surface
  (`start_task`/`poll_task_status`/`handle_elicitation`/`get_task_result` + `get_pending_decision`); the
  PO workflow itself is unchanged.
- **`test_ui_prompt.py`**, **`test_gui.py`**: no change expected (preserved shapes); run to confirm.

### Layer 6 — Docs
- Update CLAUDE.md (this file describes the v2 protocol throughout — flip it to v1/FastMCP).
- Add an ADR recording the v2→v1 reversal and why; flip the "superseded" banners so the v1 design docs
  are current again; update `mcp_tasks_temporal/README.md` to the FastMCP-backed package; re-flag the
  reader-release concurrency caveat as **active** (cite `tasks-protocol-gaps.md` + SEP-2322; note no
  merged SDK PR). Save a copy of this plan to `docs/plans/` (per project convention) once out of plan mode.

---

## Risks & mitigations
1. **`get_pending_input` shape** silently breaks the UIs — handler builds the keyed v2 shape; add a
   workflow test asserting the exact nested structure.
2. **Wrong gate routing** — inject `x-request-key`; handler selects `decision[x-request-key]`; test with
   a decision dict carrying both keys.
3. **`wait_result` blocks across the cost-center gate** — only works because the client cancels
   `tasks/result` after each elicitation; port v1's `asyncio.wait(FIRST_COMPLETED)` + cancel exactly;
   the two-gate test proving the SECOND elicitation fires is the guard.
4. **Sandbox leak from fastmcp** — keep `workflows.py`/`models.py` mcp-free + string activity refs;
   needing `with_passthrough_modules("fastmcp","mcp")` means fix the leak, not pass it through.
5. **Plugin/FastMCP construction ordering** — build activities first, FastMCP client (with bound
   handler) in `run_context`, then set `_mcp`.
6. **`durable_sync_mcp` stays orphaned** — out of scope; note it needs `mcp.server.fastmcp` → `fastmcp`
   to revive (not in `testpaths`).

---

## Verification (end-to-end)
1. `temporal server start-dev`
2. `python -m bizservice.worker` (InvoiceWorkflow + PayLineItem + activities)
3. `python -m invoice_processing_mcp.client.worker --config invoice_processing_mcp/client_config.json`
   (the FastMCP client spawns `python -m invoice_processing_mcp.server` over stdio — no extra terminal)
4. `python -m invoice_processing_mcp.client.gui --port 8080` → http://localhost:8080

- **Simple (one gate):** "Submit simple" → row `working` → `input_required`; the question pane shows the
  approve/reject enum (proves keyed `get_pending_input`). Approve → `completed`; `get_progress` shows
  back-office steps ran concurrently.
- **Large (BOTH gates):** "Submit large" → `input_required` (approval) → approve → brief `working`
  (RECONCILING) → `input_required` **again** (cost-center `{cost_center, memo}`). This second prompt is
  the critical proof the first `tasks/result` was cancelled and polling resumed. Fill + submit →
  `working` (PAYING) → `completed`.
- **CLI cross-check:** `python -m invoice_processing_mcp.client.ui` → `submit samples/invoice_large.json`
  → `list` drives the approval, then the multi-field cost-center form via `_prompt_for`.
- **Concurrency:** submit "large" twice quickly so two tasks sit in `input_required` at once; both must
  be answerable independently (verifies the reader-release prevents starvation on the shared session).
- **Automated:** `uv run pytest mcp_tasks_temporal/tests invoice_processing_mcp/tests` — all green,
  especially the two-gate `test_server`/`test_client_workflow` and the multi-key `test_client_activities`.
