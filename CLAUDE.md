# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an invoice processing demo that integrates Temporal workflows with the Model Context Protocol (MCP). It implements the **MCP Tasks extension** (`io.modelcontextprotocol/tasks`, the 2026-07-28 draft) — Temporal-backed — for async long-running operations with human-in-the-loop approval.

The Tasks extension is implemented generically and reusably in **`mcp_tasks_temporal/`** and distributed as a Temporal Plugin; the invoice app is its first consumer. The `mcp==2.0.0a2` SDK deliberately omits the extension, so the wire types and handlers are hand-written — see `docs/research/v2-alpha-spike-findings.md` and `docs/plans/temporal-tasks-extension.md`.

The repo contains a shared business service (`bizservice/`) and MCP server implementations:
- `invoice_processing_mcp/` — the invoice server + durable client, built on the Temporal tasks extension (`mcp` v2)
- `durable_sync_mcp/` — synchronous tools (no Tasks) for Claude Desktop; uses the SDK-vendored `mcp.server.fastmcp` (removed in v2), **not migrated** — fails to import under the v2 cutover (see note below)

The client side is `invoice_processing_mcp/client/` — a durable Temporal-based client that adopts `mcp_tasks_temporal`'s plugin. (An older LLM-driven CLI client was removed in the v2 migration.)

## Repository Structure

```
bizservice/              Temporal workflows, activities, worker, CLI (pure Temporal; unchanged)
mcp_tasks_temporal/      Reusable Temporal-backed MCP Tasks extension (io.modelcontextprotocol/tasks)
  wire.py                Pydantic wire types for the extension
  _sdk_compat.py         Alpha shim: lets a bare CreateTaskResult pass tools/call validation
  server.py              register_tasks_extension(server, backend) — wires tasks/* onto a lowlevel Server
  backend.py             TaskBackend protocol + TaskState (MCP task state you map workflow state onto)
  client/                Durable client: workflows.py, activities.py, session.py, models.py
  plugin.py              MCPTasksClientPlugin — the Temporal Plugin packaging the client
  tests/                 Unit + in-memory-transport + time-skipping tests
invoice_processing_mcp/  Invoice app (consumer of mcp_tasks_temporal) — two components:
  server/                The MCP server
    server.py            Lowlevel MCP server; process_invoice via the tasks extension
    invoice_backend.py   InvoiceTaskBackend — TaskBackend backed by InvoiceWorkflow (task ID = workflow ID)
    __main__.py          `python -m invoice_processing_mcp.server`
  client/                The client application
    worker.py            Durable MCP client = Temporal worker: MCPTasksClientPlugin + PurchaseOrderWorkflow & back-office activities
    purchase_order_workflow.py  PurchaseOrderWorkflow — parent orchestrator; runs the MCP task as a child TaskTrackerWorkflow while doing back-office work concurrently
    backoffice_activities.py    mcp-free activities (goods receipt, inventory, notify, close PO) the parent runs alongside payment
    ui.py                Interactive CLI (Temporal only); submits purchase orders, renders inputRequests
    gui.py               NiceGUI status board (Temporal only); lists POs + live invoice-task state, answers HITL inline. `python -m invoice_processing_mcp.client.gui`
    __main__.py          `python -m invoice_processing_mcp.client` (defaults to UI)
  client_config.json     MCP client config (Claude Desktop format)
  boot-demo.sh           tmux helper to start server + worker
  tests/                 test_invoice_backend.py (backend mapping + 2-gate server wiring); test_purchase_order_workflow.py; test_ui_prompt.py
durable_sync_mcp/        Synchronous-tools server using SDK-vendored mcp.server.fastmcp (removed in v2; NOT migrated — see note)
  server.py              FastMCP server with individual tools for Claude Desktop
  claude_desktop_config.json  Sample Claude Desktop config
  README.md              Setup and usage instructions
samples/                 Sample invoice JSON (invoice1.json: $1000 → one gate; invoice_large.json: $12.5k → both gates)
docs/                    Design docs, research, and plans
```

> **durable_sync_mcp note:** it imports `mcp.server.fastmcp.FastMCP` — the FastMCP that was vendored
> into the SDK in v1 and **removed in `mcp 2.0.0a2`** — so it fails to import under the v2 cutover. It
> is left unmigrated; to revive it, port it to `mcp.server.mcpserver.MCPServer` (or the lowlevel
> `Server`), or run it in a separate venv pinned to `mcp 1.x`. (Distinct from the *standalone*
> `fastmcp` package, which the now-removed legacy CLI client used and which is also uninstalled.)

## Commands

### Setup
```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"   # installs mcp==2.0.0a2 (pinned), temporalio, dev tools
```

### Running the Demo
```bash
# Terminal 1: Start Temporal server
temporal server start-dev

# Terminal 2: Start the bizservice worker (processes invoices)
python -m bizservice.worker [--fail-validate] [--fail-payment]

# Terminal 3: MCP server (also launched over stdio by the client per client_config.json;
#             running it standalone is optional)
python -m invoice_processing_mcp.server

# Terminal 4: client-side Temporal worker (durable MCP tasks client)
python -m invoice_processing_mcp.client.worker [--config invoice_processing_mcp/client_config.json]

# Terminal 5: UI (connects to Temporal only)
python -m invoice_processing_mcp.client.ui
# or: python -m invoice_processing_mcp.client  (defaults to UI)
#   submit samples/invoice_large.json   → $12.5k, exercises BOTH HITL gates
#   submit samples/invoice1.json        → $1000, approval gate only

# Optional: NiceGUI status board (connects to Temporal only) at http://localhost:8080
python -m invoice_processing_mcp.client.gui [--port 8080 --refresh-seconds 2.0]

# Or use the helper script (requires tmux)
./invoice_processing_mcp/boot-demo.sh
```

### Running Tests
```bash
uv run pytest                                            # all (testpaths: mcp_tasks_temporal/tests, invoice_processing_mcp/tests)
uv run pytest mcp_tasks_temporal/tests/test_server.py -q # one file
uv run pytest path/to/test.py::test_name                 # single test
```

### Linting/Formatting
```bash
black .
isort .
flake8
mypy .
```

## Architecture

### Workflows (`bizservice/workflows.py`)
- **InvoiceWorkflow** — Main orchestrator. Validates invoice, waits for approval (up to 5 days), reconciles with the ERP (slow step), then for invoices over `COST_CENTER_THRESHOLD` ($5000) takes a **second HITL gate** (cost-center coding) before paying line items in parallel via child workflows. Status: INITIALIZING -> PENDING-VALIDATION -> PENDING-APPROVAL -> APPROVED -> RECONCILING -> [PENDING-COST-CENTER -> CODED] -> PAYING -> PAID/FAILED (or REJECTED). Signals: `ApproveInvoice`, `RejectInvoice`, `SubmitCostCenter(content)`. Queries: `GetInvoiceStatus`, `GetInvoiceData`, `GetCostCenter`.
- **PayLineItem** — Child workflow that waits until due date, then calls payment gateway with retry policy (3 attempts, non-retryable for INSUFFICIENT_FUNDS). Signal: `ForcePayment` skips the due-date timer and pays immediately.

### Activities (`bizservice/activities.py`)
- `validate_against_erp` — Validates invoice (30% random failure, or forced via `FAIL_VALIDATE=true`, disabled via `NO_FAIL_VALIDATE=true`)
- `payment_gateway` — Processes payment (5% INSUFFICIENT_FUNDS, 30% retryable failure, or forced via `FAIL_PAYMENT=true`, disabled via `NO_FAIL_PAYMENT=true`)
- `reconcile_with_erp` — Posts the approved invoice to the ERP; an intentionally slow step (delay tunable via `RECONCILE_DELAY_SECONDS`, default 15s) so the client polls the task several times before the next gate

### Worker (`bizservice/worker.py`)
Connects to Temporal and polls `invoice-task-queue` for both workflows and activities. Supports `--fail-validate` and `--fail-payment` flags to force failures for testing.

### MCP Server (`invoice_processing_mcp/server/`)

`server.py` builds a lowlevel `mcp.server.lowlevel.Server`, exposes `process_invoice` via
`on_list_tools`, and calls `register_tasks_extension(server, InvoiceTaskBackend(client), task_tools={"process_invoice"})`, then serves over stdio.

`InvoiceTaskBackend` (`invoice_backend.py`) implements `TaskBackend` against Temporal — **the workflow ID is the MCP task ID** (1:1, no lookup table):
- `start` → starts `InvoiceWorkflow` on `invoice-task-queue`, returns the initial (`working`) `TaskState`
- `get_state` → **maps workflow state → task state**: queries `GetInvoiceStatus` and maps it via `TEMPORAL_TO_MCP_STATE`; on `PENDING-APPROVAL` builds the approval `inputRequests` (key `approval`, an approve/reject `elicitation/create`); on `PENDING-COST-CENTER` builds the cost-center `inputRequests` (distinct key `cost-center-coding`, a multi-field data-entry form); on `PAID`/`REJECTED` the terminal `result`; on `FAILED` an `error`
- `submit_input` → routes by key: `approval` → `ApproveInvoice`/`RejectInvoice`; `cost-center-coding` → `SubmitCostCenter(content)` (or `RejectInvoice` on decline/cancel)
- `cancel` → cancels the workflow

**Task Lifecycle (v2 extension — pure polling; elicitation is data, not a server push):**
```
Client                          MCP Server                      Temporal
  |-- tools/call (ext _meta) --->|                               |
  |   process_invoice            |-- start_workflow ------------>|  (taskId = workflow_id)
  |<-- CreateTaskResult ---------|  (status: working)            |
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: working ----------|<-- PENDING-VALIDATION --------|
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- input_required + ---------|<-- PENDING-APPROVAL ----------|
  |    inputRequests{approval}   |                               |
  |-- tasks/update ------------->|-- signal ApproveInvoice ----->|
  |    inputResponses{approval}  |<-- (empty ack) ---------------|
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: working ----------|<-- RECONCILING (slow) --------|   (client polls several times)
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- input_required + ---------|<-- PENDING-COST-CENTER -------|   (2nd gate, large invoices only)
  |    inputRequests             |                               |
  |      {cost-center-coding}    |                               |
  |-- tasks/update ------------->|-- signal SubmitCostCenter --->|
  |    inputResponses{cost-...}  |<-- (empty ack) ---------------|
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- completed + result -------|<-- PAID ----------------------|
```

**Client-side orchestration (`PurchaseOrderWorkflow`):** the UI's `submit` starts a
`PurchaseOrderWorkflow`, which records goods receipt, starts the `process_invoice` MCP task as a
**child `TaskTrackerWorkflow`**, then runs back-office activities (inventory, requester notification,
PO close) **concurrently** (`asyncio.gather`) while the payment task is pending/awaiting input. HITL is
still driven against the child tracker directly (the UI signals its `user_decision`); the parent just
awaits it. `get_progress` exposes back-office steps done + payment status + the child workflow id.

### Sync MCP Server (`durable_sync_mcp/server.py`)

A simpler MCP server where the LLM (Claude Desktop) orchestrates the multi-step invoice flow directly via individual tool calls. No MCP Tasks, no elicitation — the agent decides when to check status, approve, or reject.

**Tools:**
- `process_invoice` — Starts a Temporal workflow, returns `workflow_id` + `run_id`
- `approve_invoice` — Signals `ApproveInvoice` on a workflow
- `reject_invoice` — Signals `RejectInvoice` on a workflow
- `invoice_status` — Queries `GetInvoiceStatus` + workflow description

**Interaction Flow:**
```
Claude Desktop                  MCP Server                    Temporal
  |                               |                            |
  |-- tools/call --------------->|                            |
  |  (process_invoice)            |-- start_workflow --------->|
  |<-- {workflow_id, run_id} -----|                            |
  |                               |                            |
  |-- tools/call --------------->|-- query GetInvoiceStatus ->|
  |  (invoice_status)             |<-- PENDING-APPROVAL -------|
  |<-- "PENDING-APPROVAL" -------|                            |
  |                               |                            |
  |  (asks user, user says yes)   |                            |
  |-- tools/call --------------->|-- signal ApproveInvoice -->|
  |  (approve_invoice)            |                            |
  |<-- "APPROVED" ---------------|                            |
```

### Tasks Extension (`mcp_tasks_temporal/`)

A reusable, Temporal-backed implementation of the MCP **Tasks extension** (`io.modelcontextprotocol/tasks`), hand-written because `mcp==2.0.0a2` deliberately omits it (`mcp.types.methods` comments `tasks/* deliberately absent`). The invoice app is its first consumer.

**Usage guide for building your own tasks server/client with this package:** [`mcp_tasks_temporal/README.md`](mcp_tasks_temporal/README.md).

- **`wire.py`** — Pydantic types: `CreateTaskResult`, `GetTaskResult` (DetailedTask variants), `InputRequest`/`InputResponse`, the `tasks/*` request-param models, and capability helpers (`client_capability_meta`, `declares_tasks_extension`). camelCase on the wire.
- **`_sdk_compat.py`** — `install_tasks_result_passthrough()`: a narrow, guard-tested seam over the SDK's `serialize_server_result` (server) / `validate_server_result` (client) so a bare `CreateTaskResult` (`resultType:"task"`) passes `tools/call` validation. `tasks/*` are unknown to the SDK and pass through untouched. **This is the one core gap to upstream** (widen the tools/call result surface, or add a result-type registration hook) — see `mcp_tasks_temporal/README.md` → "Path to native"; not throwaway.
- **`server.py`** — `register_tasks_extension(server, backend, *, task_tools=None, fallback_call_tool=None)`: installs the shim, advertises the capability, wraps `tools/call` (capability-gated → `CreateTaskResult`), and registers `tasks/get`/`tasks/update`/`tasks/cancel` via `server.add_request_handler(method, ParamsModel, handler)` on a `mcp.server.lowlevel.Server`. Delegates app behavior to a `TaskBackend`.
- **`backend.py`** — `TaskBackend` protocol (`start`/`get_state`/`submit_input`/`cancel`) + `TaskState` (the MCP-facing task state a backend maps its workflow state onto).
- **`client/`** — the durable client: `workflows.TaskTrackerWorkflow` (poll → surface `inputRequests` → await `user_decision` → `tasks/update` → terminal), `activities.MCPActivities` (`start_task`/`poll_task`/`submit_task_input`/`cancel_task` over a shared session), `session.py` (stdio session + per-request capability `_meta`; result retrieval folded into `poll_task`), `models.py` (sandbox-safe dataclasses + activity-name string constants).
- **`plugin.py`** — `MCPTasksClientPlugin(server_params)`: a Temporal `SimplePlugin` bundling the workflow + activities + the MCP session lifecycle (via `run_context`). One `plugins=[...]` entry adopts the whole durable client.

### Client Application (`invoice_processing_mcp/client/`)

The invoice consumer of the durable client; the reusable parts live in `mcp_tasks_temporal`.

- **`worker.py`** — reads the Claude Desktop-format config, builds `StdioServerParameters`, and runs `Worker(client, task_queue=models.TASK_QUEUE, workflows=[PurchaseOrderWorkflow], activities=[<back-office>], plugins=[MCPTasksClientPlugin(server_params)])`. The plugin's `TaskTrackerWorkflow` + wire activities are **merged** with these explicit lists (do **not** re-list `TaskTrackerWorkflow`).
- **`purchase_order_workflow.py`** — `PurchaseOrderWorkflow`, the parent orchestrator: records goods receipt, starts `process_invoice` as a **child `TaskTrackerWorkflow`** (`workflow.start_child_workflow`, child id `task-tracker-{workflow.uuid4()}`), then `asyncio.gather`s the child handle with the back-office work so they run **concurrently**. Query `get_progress` returns `{po_id, steps_done, payment_status, payment_workflow_id}` (the child task id). The MCP task is one step of a larger durable business process, not the whole thing.
- **`backoffice_activities.py`** — `mcp`-free activities (`record_goods_receipt`, `update_inventory`, `notify_requester`, `close_po`) the parent runs alongside payment; per-step delay via `BACKOFFICE_DELAY_SECONDS` (default 2s). Kept `mcp`-free so the sandbox re-import of the parent stays clean; referenced by function object.
- **`ui.py`** — Temporal-only CLI. `submit <file>` starts a `PurchaseOrderWorkflow`; `list` shows running POs with `get_progress` and renders any pending `inputRequests` from the child trackers (still discovered by `WorkflowType = "TaskTrackerWorkflow"`), then signals `user_decision`. `_prompt_for` renders arbitrary multi-field schemas (optional fields skippable, enums validated).
- **`gui.py`** — Temporal-only NiceGUI status board. Lists **all** `PurchaseOrderWorkflow`s (running + closed, newest first) via `list_workflows`; per PO, resolves the child task id from `get_progress` and shows the child's live `get_status`; auto-refreshes every `--refresh-seconds` (default 2s ≈ the server poll interval). Clicking an `input_required` status renders that task's pending question (from the child's `get_pending_input`) in a pane above the list; **Submit** signals `user_decision` (reject by choosing `reject` in the form), **Close** dismisses without answering. A submit pane (**Submit simple/large**) starts a PO from a sample with a randomized `invoice_id` + ±10% line amounts. Temporal-facing helpers (`fetch_po_rows`, `build_responses`, `submit_decision`, `randomize_invoice`, `start_po`) are factored out and unit-tested (`tests/test_gui.py`); rendering is verified manually.

**No elicitation callback, no concurrency hack.** Because v2 surfaces input as data in `tasks/get` and answers it via `tasks/update`, the old `_elicitation_handler`, `_active_elicitations`, `x-task-id` smuggling, and 20ms-raise workaround are all gone. HITL is just "store `inputRequests` → await a signal → `tasks/update`" inside the workflow.

### Legacy MCP CLI Client — REMOVED
The old LLM-driven CLI (formerly `async_mcp/mcp_client/`, OpenAI Responses API + standalone `fastmcp`) was **deleted** in the v2 migration. Some historical design/plan docs under `docs/` still reference it for context.

## Key Patterns

- **Tasks is an extension we implement, not an SDK feature.** `mcp==2.0.0a2` ships the `extensions` capability container but no tasks code; we hand-define the wire types and register `tasks/*` on the lowlevel server (this package is intended to *be* the canonical tasks implementation). New methods pass through unvalidated; `tools/call` needs the `_sdk_compat` seam to return a bare `CreateTaskResult`. That seam is the one core gap "native tasks" must close upstream (widen the tools/call result surface, or add a result-type registration hook) — not throwaway, but the thing to upstream; the handlers/client are already final. See `mcp_tasks_temporal/README.md` → "Path to native".
- **Server-side: workflow ID = MCP task ID** (no lookup table). **Client-side:** `TaskTrackerWorkflow` ID is `task-tracker-{uuid}` (now minted by `PurchaseOrderWorkflow` via `workflow.uuid4()` and exposed through its `get_progress`), distinct; the MCP task ID lives in workflow state. There is no `tasks/list` in v2 — Temporal `list_workflows` is the durable task registry.
- **Capability negotiation is per-request `_meta`**: the client adds `_meta.io.modelcontextprotocol/clientCapabilities.extensions["io.modelcontextprotocol/tasks"]`; the server gates task creation on it (`declares_tasks_extension`) and MUST NOT create a task otherwise.
- **HITL via Tasks (no server push)**: `PENDING-APPROVAL`/`PENDING-COST-CENTER` → `tasks/get` carries `inputRequests`; the client answers via `tasks/update` (`inputResponses`), which signals the workflow. **Multi-round HITL** uses distinct keys per round (`approval`, then `cost-center-coding`) — the Tasks spec requires `inputRequests` keys be unique over a task's lifetime; `TaskTrackerWorkflow` resets `_decision`/`_pending_input` each round so it re-enters `input_required` cleanly.
- **Parent orchestrator + child task**: `PurchaseOrderWorkflow` runs the MCP task as a child `TaskTrackerWorkflow` while doing other durable work concurrently — the durable-orchestration value story. The plugin's workflows/activities **merge** with the explicit `workflows=`/`activities=` on the same `Worker` (append; do not re-list `TaskTrackerWorkflow`). Verified on `temporalio` 1.29.
- **Sandbox cleanliness**: `TaskTrackerWorkflow` imports only `mcp_tasks_temporal.client.models` (pure dataclasses) and references activities by string name, so `mcp` never loads in the workflow sandbox. `PurchaseOrderWorkflow` imports its back-office activities under `with workflow.unsafe.imports_passed_through():` and those activities are `mcp`-free. Package `__init__` files are kept import-light for the same reason. Worker passes `SandboxedWorkflowRunner(restrictions=...with_passthrough_modules("beartype"))`.
- Invoice JSON: `{"invoice_id": str, "customer": str, "lines": [{"description": str, "amount": number, "due_date": ISO8601}]}`
- Temporal address via `TEMPORAL_ADDRESS` (default `localhost:7233`); client config uses Claude Desktop format `{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}}}`.
- **Testing**: extension + server tested end-to-end over the in-memory MCP transport (`mcp.shared.memory.create_client_server_memory_streams`), including a two-gate flow; `TaskTrackerWorkflow` and `PurchaseOrderWorkflow` under `WorkflowEnvironment.start_time_skipping()` (the PO test asserts back-office work completes *while* payment is pending, then drives the child). The time-skipping test server has **no visibility API** — `list_workflows` is unimplemented — so the PO test reads the child id from the parent's `get_progress` rather than querying by type. The real-workflow E2E is the manual demo — `InvoiceWorkflow`'s 5-day approval timer makes the time-skipping env auto-approve, so it isn't in the suite.
- `mcp==2.0.0a2` is pinned (unpinned resolves to v1); `temporalio>=1.19` for the Plugins API; `nicegui` for the GUI status board.
