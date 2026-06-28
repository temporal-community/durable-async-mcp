# Plan: Richer invoice workflow (#2) + client-side PO orchestrator

> Status: **implemented 2026-06-27.** Captured here as project memory (the approved plan).

## Context

The v2 MCP Tasks-extension migration is done. The demo was thin in two ways:

1. The MCP task was only ~3 steps (validate → approve → pay) with a single HITL gate keyed
   `"approval"` — no multi-round HITL, no visible polling. (Roadmap item #2.)
2. The client only *traced* the task: `TaskTrackerWorkflow` was started top-level by the UI and did
   nothing but follow one MCP task — no business process around it, undersell­ing the Temporal value
   story (durable orchestration of a process that has *other* work to do while an external async task
   is pending).

This change added both halves of a realistic purchase-to-pay demo:

- **Server (B):** `InvoiceWorkflow` is richer — after approval it runs a slow `reconcile_with_erp`
  step (client polls several times), then for invoices over `COST_CENTER_THRESHOLD` enters a second,
  distinct HITL gate (`cost-center-coding`, a data-entry form, not approve/reject) before paying.
- **Client (A):** a new invoice-domain **`PurchaseOrderWorkflow`** parent orchestrator. On goods
  receipt it starts `process_invoice` as a **child `TaskTrackerWorkflow`**, then runs back-office
  steps (inventory, requester notification, PO close) **concurrently** while the payment task is
  pending/awaiting input, finishing when both complete.

### Decisions (from the user)
- Scenario: **PO / order fulfillment**.
- `TaskTrackerWorkflow` runs as a **child** of the parent (unchanged; children stay independently
  queryable/signalable, so the existing UI HITL path still works).
- Scope: **B + A only**; the generic Claude agent (item #1) is deferred.

### Key facts verified (temporalio 1.29)
- A single `Worker` MERGES `plugins=[...]` workflows/activities with explicit `workflows=`/`activities=`
  (append). We add `PurchaseOrderWorkflow` + back-office activities to the explicit lists and keep the
  plugin. Do **not** re-list `TaskTrackerWorkflow` (the plugin already registers it).
- Use `workflow.start_child_workflow(...)` (returns an awaitable `ChildWorkflowHandle`), then
  `await asyncio.gather(child_handle, side_work_coro)`. Passing the typed `.run` method means no
  `result_type` is needed (mypy rejects it on the typed overload).
- Generate the child id with `workflow.uuid4()` inside the workflow.
- `TaskTrackerWorkflow` already resets `_decision`/`_pending_input` each round, so two sequential
  `input_required` rounds with different keys work with no tracker change.
- The time-skipping test server has **no visibility API** (`list_workflows` is unimplemented), so the
  parent exposes the child id via `get_progress` for the test to signal it.

## Change B — server side
- `bizservice/activities.py`: `reconcile_with_erp` (slow; delay tunable via `RECONCILE_DELAY_SECONDS`,
  default 15s).
- `bizservice/workflows.py`: states `RECONCILING` → (if `total > COST_CENTER_THRESHOLD=5000`)
  `PENDING-COST-CENTER` → `CODED`; signal `SubmitCostCenter(content)`; query `GetCostCenter`.
- `bizservice/worker.py`: register `reconcile_with_erp`.
- `invoice_processing_mcp/server/invoice_backend.py`: map new states; `COST_CENTER_KEY =
  "cost-center-coding"`; `_cost_center_request` (multi-field `cost_center` + optional `memo`);
  `get_state` branches the input_required gate by status; `submit_input` routes by key.

## Change A — client side
- `invoice_processing_mcp/client/backoffice_activities.py` (mcp-free): `record_goods_receipt`,
  `update_inventory`, `notify_requester`, `close_po` (delay via `BACKOFFICE_DELAY_SECONDS`, default 2s).
- `invoice_processing_mcp/client/purchase_order_workflow.py`: `PurchaseOrderWorkflow` — goods receipt
  → start child tracker → `asyncio.gather(child, back-office)` → combined result; `get_progress` query
  (steps done, payment status, child workflow id).
- `invoice_processing_mcp/client/worker.py`: add the parent workflow + back-office activities to the
  explicit lists (merged with the plugin).
- `invoice_processing_mcp/client/ui.py`: `submit` starts a `PurchaseOrderWorkflow`; `list` shows POs
  with `get_progress` (HITL discovery still scans `TaskTrackerWorkflow`); `_prompt_for` iterates all
  schema properties (multi-field forms, optional fields skippable, enums validated).

## Tests
- `invoice_processing_mcp/tests/test_invoice_backend.py`: cost-center request/route tests + a two-gate
  in-memory E2E.
- `invoice_processing_mcp/tests/test_purchase_order_workflow.py`: time-skipping test asserting the
  back-office work completes while payment is still pending (concurrency), then drives the child.
- `invoice_processing_mcp/tests/test_ui_prompt.py`: multi-field / optional / enum prompt rendering.

## Verification (manual demo)
1. `temporal server start-dev`
2. `python -m bizservice.worker`
3. `python -m invoice_processing_mcp.client.worker`
4. `python -m invoice_processing_mcp.client.ui`
5. `submit samples/invoice_large.json` (>$5000 → hits both gates). Watch the back-office activities run
   in the Temporal UI **while** the child tracker is in approval/reconcile/coding.
6. `list` → approve (gate 1) → several polls during `reconcile_with_erp` → answer the cost-center form
   (gate 2, multi-field) → child reaches PAID and the PO returns `fulfilled`.
   (`samples/invoice1.json` totals $1000 and skips the second gate.)
