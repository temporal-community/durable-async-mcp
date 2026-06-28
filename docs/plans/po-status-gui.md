# Plan: NiceGUI status board for PurchaseOrder / invoice tasks

## Context

The project's thesis is now "**MCP Tasks is a standardized way of dealing with async tools**" — the
existing implementation already makes that point (typed wire protocol in `mcp_tasks_temporal/wire.py`,
`tasks/*` on the lowlevel server, a durable client that speaks the standard, capability negotiation via
`_meta`). No protocol work is needed.

What's missing is a **visual** way to watch the standard in motion. Today the only client surface is a
CLI (`invoice_processing_mcp/client/ui.py`) that prints a list on `list` and prompts on stdin. This
change adds a **browser GUI** (NiceGUI, pure-Python) that shows every purchase order and the live
lifecycle state of its invoice task, and lets you answer pending human-in-the-loop questions inline —
a much better demo/talk visual for "the task moves through `working → input_required → completed`."

The GUI is a **Temporal client only** (like `ui.py`): it lists workflows, queries their state, and
signals decisions. It reuses the exact mechanism the CLI uses; nothing about the server or the
protocol changes.

### Decisions (from user)
- Toolkit: **NiceGUI** (browser, pure-Python).
- Keep the existing CLI `ui.py`; the GUI is an **additional** entry point.
- Clicking an `input_required` row **poses the question in a pane above the list** and lets the user
  **answer + submit** (signaling the child task's `user_decision`) — otherwise the task can't advance.

### Required GUI behavior (from user)
- List **all** `PurchaseOrderWorkflow`s, running *and* completed; newest first.
- Each row shows the **invoice task's** lifecycle state (the child `TaskTrackerWorkflow`'s status:
  `working` / `input_required` / `completed` / `failed` / `cancelled`).
- Auto-refresh on ~the task poll interval (server `POLL_INTERVAL_MS = 2000` → **2s**, configurable).
- When a row is `input_required`, the status text is **clickable**; clicking renders that task's pending
  question(s) in the **upper pane**. Clicking a different `input_required` swaps the pane to it.

## Data flow (reuses existing signatures)

Per refresh tick (every ~2s), for each `PurchaseOrderWorkflow` execution:
1. `client.list_workflows('WorkflowType = "PurchaseOrderWorkflow"')` → all executions (no status
   filter; closed workflows are included). Sort by `execution.start_time` desc.
2. `handle.query(PurchaseOrderWorkflow.get_progress)` → `payment_workflow_id` (the child task id) +
   `po_id` (see small edit below) + `steps_done`.
3. `client.get_workflow_handle(child_id).query(TaskTrackerWorkflow.get_status)` → the **task lifecycle
   state** shown in the row. (Queries work on closed workflows too.)
4. When the user clicks an `input_required` row: `…get_workflow_handle(child_id).query(
   TaskTrackerWorkflow.get_pending_input)` → the `inputRequests` dict (`{key: {method, params}}`,
   `params` carries `message` + `requestedSchema`) → render in the upper pane.
5. On submit: build `inputResponses` `{key: {"action": "accept"|"decline", "content": {...}}}` and
   `…get_workflow_handle(child_id).signal(TaskTrackerWorkflow.user_decision, responses)`.

Child id is resolved from the **parent's** `get_progress` (the PO mints it via `workflow.uuid4()`), so
the GUI never needs the `WorkflowType = "TaskTrackerWorkflow"` discovery the CLI uses.

## Changes

### Small edit — surface `po_id` for a friendly label
`invoice_processing_mcp/client/purchase_order_workflow.py`: store `self._po_id = order.get("po_id")`
in `run()` and add it to the `get_progress` return dict (`po_id`, alongside `steps_done`,
`payment_status`, `payment_workflow_id`). Additive; existing tests unaffected.

### New `invoice_processing_mcp/client/gui.py`
A NiceGUI app, run via `python -m invoice_processing_mcp.client.gui` (new `__main__`-style entry; CLI
flags `--temporal-address`, `--port`, `--refresh-seconds` default 2.0). Structure:

- **Temporal client** connected once in `app.on_startup` (async), stored module-level.
- **Layout:** a top container = the **question pane** (`@ui.refreshable`), below it the **PO list**
  (`@ui.refreshable`). Module-level `selected_child_id` holds the clicked task.
- **List rendering:** one row per PO showing `po_id` / PO workflow status / task status. When task
  status is `input_required`, render the status as a `ui.link`/clickable label with
  `on_click=lambda cid=child_id: select(cid)`; otherwise a plain (optionally color-coded) label.
- **`select(child_id)`** sets `selected_child_id` and calls `question_pane.refresh()`.
- **Question pane:** if `selected_child_id` set, query `get_pending_input` and for each request key
  render `params["message"]` + a form built from `requestedSchema.properties` (enum → `ui.select`;
  other → `ui.input`; required vs optional per `requestedSchema.required`), plus **Submit** (action
  `accept`) and **Decline** (action `decline`) buttons. On click → build responses → signal → clear
  selection → refresh. If the selected task is no longer `input_required`, show "no longer awaiting
  input."
- **Timer:** `ui.timer(refresh_seconds, refresh_all)` where `refresh_all` re-fetches rows, refreshes
  the list, and refreshes the pane. Use a simple in-flight guard so slow ticks don't overlap.
- `ui.run(reload=False, port=...)`.

The schema→form logic mirrors `ui.py`'s `_prompt_for` (enum/free-text, optional-skippable), expressed
as NiceGUI widgets instead of `input()`.

### `pyproject.toml`
Add `nicegui` to dependencies.

## Testable helpers + tests (`invoice_processing_mcp/tests/test_gui.py`)
Factor the Temporal-facing and form logic out of the rendering so they're unit-testable with fakes
(mirror the `FakeClient`/`FakeHandle` pattern in `test_invoice_backend.py`):
- `build_responses(requested_schema, values, action) -> dict` (pure) — covers enum + multi-field +
  optional-skip + decline. Assert the `{key: {action, content}}` shape.
- `fetch_po_rows(client) -> list[dict]` (async, takes a client) — fake client returns PO executions
  whose `get_progress` yields a `payment_workflow_id`, and child handles whose `get_status` yields the
  task state; assert rows map PO → task status, including a completed PO and a `payment_workflow_id is
  None` (just-started) row.
- `submit_decision(client, child_id, responses)` (async) — assert it signals `user_decision` on the
  child handle with the right payload.

NiceGUI rendering, clicking, and the timer are verified **manually** (it's a UI). Run `uv run pytest`.

## Verification (manual)
1. `temporal server start-dev`
2. `python -m bizservice.worker`
3. `python -m invoice_processing_mcp.client.worker`
4. `python -m invoice_processing_mcp.client.gui` → open the printed `http://localhost:<port>`.
5. Submit a PO (via the existing CLI `ui.py` `submit samples/invoice_large.json`, or a small
   "submit" affordance if added later). Watch the row's task status advance roughly every 2s:
   `working → input_required`. Click `input_required` → the approval question appears in the top pane
   → approve → status proceeds through `working` (ERP reconcile) → `input_required` again
   (cost-center, for the large invoice) → answer → `completed`. Confirm completed POs remain in the
   list with task status `completed`.

## Docs to update
- `invoice_processing_mcp/README.md` and top-level `README.md` — add the GUI as a 5th process and how
  to open it.
- `CLAUDE.md` — new `client/gui.py` entry point + `python -m invoice_processing_mcp.client.gui`;
  `nicegui` dependency; note `get_progress` now includes `po_id`.
- Copy this plan to `docs/plans/po-status-gui.md` (project-local planning artifact).
