# Invoice Processing MCP (FastMCP tasks, Temporal-backed)

The invoice-processing application: an **MCP server** that exposes `process_invoice` via the
**FastMCP task protocol** (SEP-1686), and a **durable client** that drives it. Both are thin
consumers of the reusable [`mcp_tasks_temporal`](../mcp_tasks_temporal/README.md) library; the
Temporal workflow ID *is* the MCP task ID. HITL is **server-push elicitation** (the server pushes
`elicitation/create` during `tasks/result`).

Two components:

- **`server/`** — the MCP server. `server.py` wires `process_invoice` to `InvoiceTaskBackend`
  (`invoice_backend.py`) via `register_tasks_extension`. It uses a Temporal *client* to start/query/
  signal `InvoiceWorkflow`; it is not a Temporal worker.
- **`client/`** — the client application:
  - `worker.py` — the durable MCP client (a Temporal worker that adopts `MCPTasksClientPlugin` and
    also registers the parent workflow + back-office activities).
  - `purchase_order_workflow.py` — **`PurchaseOrderWorkflow`**, the parent business process you
    actually start. It runs `process_invoice` as a *child* `TaskTrackerWorkflow` while doing
    back-office work concurrently.
  - `backoffice_activities.py` — the `mcp`-free side-work activities (goods receipt, inventory,
    notify requester, close PO) the parent runs alongside payment.
  - `ui.py` — an interactive CLI that talks to Temporal only.
  - `gui.py` — a NiceGUI browser status board (also Temporal-only): a live list of all purchase
    orders with each invoice task's lifecycle state, with inline answering of pending HITL questions.

The durable `TaskTrackerWorkflow` and the MCP wire activities live in `mcp_tasks_temporal`, not here.

## Processes

For a normal run you start **three** processes (plus the Temporal server), with an **optional** fourth
for the GUI. The MCP server is *not* run separately — the client worker launches it over stdio (per
`client_config.json`).

| Process | Command | What it does |
|---|---|---|
| Temporal server | `temporal server start-dev` | Orchestration backbone |
| bizservice worker | `python -m bizservice.worker` | Runs `InvoiceWorkflow` + `PayLineItem` on `invoice-task-queue` |
| client worker | `python -m invoice_processing_mcp.client.worker` | Temporal worker on `mcp-tasks-client`; adopts `MCPTasksClientPlugin`, which spawns the MCP server over stdio and owns the session |
| UI | `python -m invoice_processing_mcp.client.ui` | Interactive CLI; connects to Temporal only |
| GUI (optional) | `python -m invoice_processing_mcp.client.gui` | NiceGUI status board at `http://localhost:8080`; connects to Temporal only. An alternative to the CLI for watching/answering tasks |

> Run the MCP server standalone only for debugging: `python -m invoice_processing_mcp.server`.

### GUI status board

`python -m invoice_processing_mcp.client.gui` (flags: `--temporal-address`, `--port`,
`--refresh-seconds`, default 2s ≈ the task poll interval) opens a browser page that:

- lists **every** `PurchaseOrderWorkflow` (running and completed), newest first, each showing the
  **invoice task's** lifecycle state (`working` / `input_required` / `completed` / `failed`);
- refreshes automatically about every 2 seconds;
- makes an `input_required` status **clickable** — clicking renders that task's pending question in a
  pane above the list (a form built from the elicitation's `requestedSchema`); **Submit** signals the
  child task's `user_decision` (to reject, choose `reject` in the form), and **Close** dismisses the
  pane without answering (the task stays `input_required`). Clicking a different `input_required` row
  swaps the pane;
- has a submit pane below the list with **Submit simple** / **Submit large** buttons that start a
  purchase order from `samples/invoice1.json` / `samples/invoice_large.json`, randomizing the
  `invoice_id` (`INV-####`) and perturbing line amounts ±10% each time (so every submission is distinct
  while still hitting the same gates).

The GUI needs the **client worker** running — it queries/signals/starts workflows, but the worker
executes the tasks. (You can still submit from the CLI `ui.py` too.)

## Running the demo

```bash
temporal server start-dev                                   # terminal 1
python -m bizservice.worker                                 # terminal 2
python -m invoice_processing_mcp.client.worker             # terminal 3
python -m invoice_processing_mcp.client.ui                 # terminal 4  (or: python -m invoice_processing_mcp.client)
```

### Starting a client-side workflow

You start a workflow with the UI's **`submit <file>`** command — it kicks off a
`PurchaseOrderWorkflow` (the parent orchestrator) for the given invoice JSON. Use `list` to watch its
progress and to answer the human-in-the-loop gates. Pick a sample:

- `samples/invoice1.json` — $1000, hits the **approval** gate only.
- `samples/invoice_large.json` — $12.5k, hits **both** gates (approval, then cost-center coding).

```
Invoice Processing Client
Commands: submit <file>, list, quit

> submit samples/invoice_large.json
  Started purchase order PO-1a2b3c4d (po-<uuid>)

> list
  po-<uuid>
    back-office: goods_receipt, inventory, requester_notified, po_closed
    payment: working

Input needed for task task-tracker-<uuid>:
  Invoice INV-900 for Globex Industries ($12500.00) requires approval. ...
  value [approve / reject]: approve
  Decision sent.

> list                       # poll again; the slow ERP reconcile runs, then the 2nd gate appears
  po-<uuid>
    back-office: goods_receipt, inventory, requester_notified, po_closed
    payment: working

Input needed for task task-tracker-<uuid>:
  Invoice INV-900 ($12500.00) exceeds the auto-approval limit. Assign a cost center ...
  cost_center: CC-1000
  memo (optional): Q3 capex
  Decision sent.

> list                       # once paid, the PO completes and drops off the running list
  No running purchase orders.
```

Note the back-office steps complete *while* the payment task is still pending — the parent does other
durable work concurrently, which is the point of the orchestrator. The HITL prompts are still answered
against the child `TaskTrackerWorkflow` (the UI discovers it by type); the parent just awaits it.

## What happens (lifecycle — status polling + server-push elicitation)

1. **Start** — `submit` starts a `PurchaseOrderWorkflow`, which records goods receipt and then starts
   a *child* `TaskTrackerWorkflow(tool_name="process_invoice", arguments={...})`. The child's
   `start_task` activity calls `tools/call` with `task=True`; the server's `InvoiceTaskBackend.start`
   launches an `InvoiceWorkflow` and returns a task stub (taskId = workflow ID). The parent then runs
   its remaining back-office activities **concurrently** with the pending child (`asyncio.gather`).
2. **Validate** — the bizservice worker validates the invoice; `TaskTrackerWorkflow` polls `tasks/get`
   (`poll_task`), and `InvoiceTaskBackend.get_state` maps the workflow status → MCP task status.
3. **Approval is pushed** — at `PENDING-APPROVAL` the task is `input_required`; the child runs its
   `handle_elicitation` activity, which opens `tasks/result`. The server **pushes** an
   `elicitation/create` (the approve/reject form, key `approval`, tagged with `x-task-id` +
   `x-request-key`). The bound `_elicitation_handler` signals `elicitation_received`, surfacing the
   `inputRequests` for the UI.
4. **You decide** — `list` queries `get_pending_input`, the UI renders the prompt, and signals
   `user_decision` with the `inputResponses`. The handler reads it and returns an `ElicitResult`; the
   client then cancels `tasks/result` and resumes polling.
5. **Submit** — the elicitation answer reaches `InvoiceTaskBackend.submit_input`, which turns it into
   an `ApproveInvoice` / `RejectInvoice` signal.
6. **Reconcile + 2nd gate (large invoices)** — after approval `InvoiceWorkflow` runs the slow
   `reconcile_with_erp` step (the client polls several times), and for invoices over
   `COST_CENTER_THRESHOLD` ($5000) surfaces a **second** `input_required` round under a distinct key
   `cost-center-coding` (a multi-field form), pushed the same way and signalling `SubmitCostCenter`.
7. **Pay & complete** — the bizservice worker pays line items in parallel; the child keeps polling
   until `tasks/get` returns `completed` (`PAID`). When both the child and the back-office work finish,
   the `PurchaseOrderWorkflow` completes with `fulfilled`.

Multi-round HITL relies on each inputRequest key being unique over the task's lifetime (`approval`,
then `cost-center-coding`); the `x-request-key` carried in the pushed schema routes each answer.

## Available tools

- **`process_invoice`** — task-augmented (`task=TaskConfig(mode="required")`). `tools/call` returns a
  task handle instead of the answer; the client polls `tasks/get` for status and, on `input_required`,
  opens `tasks/result` to receive the server-pushed `elicitation/create` and answer it.

## Architecture

### `server/server.py`
`build_server(client)` creates a `FastMCP("invoice_processor")` with `process_invoice`
(`task=TaskConfig(mode="required")`) and calls `register_tasks_extension(mcp,
InvoiceTaskBackend(client), task_tools={"process_invoice"})`. Served over stdio
(`mcp.run_async(transport="stdio")`).

### `server/invoice_backend.py`
`InvoiceTaskBackend` implements the `TaskBackend` protocol — **the mapping layer between two state
models**: `InvoiceWorkflow`'s domain *workflow state* and the MCP *task state*.
- `start` → starts `InvoiceWorkflow` (queue `invoice-task-queue`), returns the initial `TaskState`
- `get_state` → maps `GetInvoiceStatus` via `TEMPORAL_TO_MCP_STATE`; on `PENDING-APPROVAL` builds the
  approval `inputRequests` (key `approval`); on `PENDING-COST-CENTER` builds the cost-center
  `inputRequests` (distinct key `cost-center-coding`, a multi-field form); on `PAID`/`REJECTED` the
  terminal `result`; on `FAILED` an `error`
- `submit_input` → routes by key: `approval` → `ApproveInvoice`/`RejectInvoice`; `cost-center-coding`
  → `SubmitCostCenter(content)` (or `RejectInvoice` on decline/cancel)
- `wait_result` → blocks until the workflow is terminal and returns its `CallToolResult` payload
- `cancel` → cancels the workflow

### `client/worker.py`
Reads `client_config.json` into a config dict and runs
`Worker(client, task_queue=models.TASK_QUEUE, workflows=[PurchaseOrderWorkflow],
activities=[<back-office>], plugins=[MCPTasksClientPlugin(config, client)])`. The plugin's
`TaskTrackerWorkflow` + wire activities are **merged** with the explicit lists — do **not** re-list
`TaskTrackerWorkflow`. The plugin owns the FastMCP session.

### `client/purchase_order_workflow.py`
`PurchaseOrderWorkflow` — the parent business process. Records goods receipt, starts `process_invoice`
as a child `TaskTrackerWorkflow` (`workflow.start_child_workflow`, child id
`task-tracker-{workflow.uuid4()}`), then `asyncio.gather`s the child handle with the back-office work
so they run concurrently. Query `get_progress` (steps done, payment status, child workflow id).

### `client/ui.py`
Temporal-only CLI: `submit <file>` starts a `PurchaseOrderWorkflow`; `list` shows running POs with
`get_progress` and renders any pending `inputRequests` from the child trackers (discovered by
`WorkflowType = "TaskTrackerWorkflow"`), then signals `user_decision`. `_prompt_for` renders arbitrary
multi-field schemas (optional fields skippable, enums validated).

## Task state mapping

| InvoiceWorkflow status | MCP task status |
|---|---|
| INITIALIZING | working |
| PENDING-VALIDATION | working |
| PENDING-APPROVAL | input_required |
| APPROVED | working |
| RECONCILING | working |
| PENDING-COST-CENTER | input_required |
| CODED | working |
| PAYING | working |
| PAID | completed |
| FAILED | failed |
| REJECTED | completed |

## Tests

```bash
uv run pytest invoice_processing_mcp/tests/   # InvoiceTaskBackend mapping + in-memory server wiring
uv run pytest mcp_tasks_temporal/tests/       # the reusable task protocol + client
```
