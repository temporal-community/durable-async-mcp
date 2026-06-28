# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an invoice processing demo that integrates Temporal workflows with the Model Context Protocol (MCP). It uses the **FastMCP task protocol** (SEP-1686, `fastmcp==2.14.3`) — Temporal-backed — for async long-running operations with human-in-the-loop approval. HITL is **server-push elicitation**: while a task is `input_required` the server pushes an `elicitation/create` during `tasks/result`, and the client answers it.

The task protocol is implemented generically and reusably in **`mcp_tasks_temporal/`** and distributed as a Temporal Plugin; the invoice app is its first consumer. It is backed by FastMCP rather than SDK-native task types. See `docs/decisions/003-revert-to-v1-fastmcp-tasks.md` for why this reverted from the v2 alpha (`mcp==2.0.0a2`) pull-based extension, and `docs/plans/v1-tasks-protocol-swap.md` for the swap plan.

The repo contains a shared business service (`bizservice/`) and MCP server implementations:
- `invoice_processing_mcp/` — the invoice server + durable client, built on the Temporal-backed FastMCP task protocol
- `durable_sync_mcp/` — synchronous tools (no Tasks) for Claude Desktop; uses `mcp.server.fastmcp` (available again under the `mcp` 1.x that fastmcp pulls in), but kept out of scope and untested

The client side is `invoice_processing_mcp/client/` — a durable Temporal-based client that adopts `mcp_tasks_temporal`'s plugin. (An older LLM-driven CLI client was removed earlier and was not restored.)

## Repository Structure

```
bizservice/              Temporal workflows, activities, worker, CLI (pure Temporal; unchanged)
mcp_tasks_temporal/      Reusable Temporal-backed FastMCP task protocol (SEP-1686)
  server.py              register_tasks_extension(mcp, backend) — overwrites FastMCP's task handlers (tools/call + tasks/get/result/cancel) with backend-driven ones
  backend.py             TaskBackend protocol + TaskState + InputRequest/InputResponse (MCP task state you map workflow state onto)
  client/                Durable client: workflows.py, activities.py, session.py, models.py
  plugin.py              MCPTasksClientPlugin — the Temporal Plugin packaging the client
  tests/                 Unit + FastMCP in-memory-transport + time-skipping tests
invoice_processing_mcp/  Invoice app (consumer of mcp_tasks_temporal) — two components:
  server/                The MCP server
    server.py            FastMCP server; process_invoice (task=required) via the tasks protocol
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
  tests/                 test_invoice_backend.py (backend mapping + single-gate in-memory wiring); test_purchase_order_workflow.py; test_ui_prompt.py; test_gui.py
durable_sync_mcp/        Synchronous-tools server using mcp.server.fastmcp (out of scope, untested — see note)
  server.py              FastMCP server with individual tools for Claude Desktop
  claude_desktop_config.json  Sample Claude Desktop config
  README.md              Setup and usage instructions
samples/                 Sample invoice JSON (invoice1.json: $1000 → one gate; invoice_large.json: $12.5k → both gates)
docs/                    Design docs, research, and plans
```

> **durable_sync_mcp note:** it imports `mcp.server.fastmcp.FastMCP`, which is available again now that
> `fastmcp==2.14.3` pulls in `mcp` 1.x. It is kept out of scope of the tasks demo and is not in the test
> suite; treat it as illustrative of the synchronous (no-Tasks) alternative for Claude Desktop.

## Commands

### Setup
```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"   # installs fastmcp==2.14.3 (pinned; pulls mcp 1.x), temporalio, dev tools
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

`server.py` builds a `FastMCP("invoice_processor")`, defines `process_invoice` as a
`task=TaskConfig(mode="required")` tool, and calls `register_tasks_extension(mcp, InvoiceTaskBackend(client), task_tools={"process_invoice"})`, then serves over stdio (`mcp.run_async(transport="stdio")`).

`InvoiceTaskBackend` (`invoice_backend.py`) implements `TaskBackend` against Temporal — **the workflow ID is the MCP task ID** (1:1, no lookup table):
- `start` → starts `InvoiceWorkflow` on `invoice-task-queue`, returns the initial (`working`) `TaskState`
- `get_state` → **maps workflow state → task state**: queries `GetInvoiceStatus` and maps it via `TEMPORAL_TO_MCP_STATE`; on `PENDING-APPROVAL` builds the approval `inputRequests` (key `approval`, an approve/reject `elicitation/create`); on `PENDING-COST-CENTER` builds the cost-center `inputRequests` (distinct key `cost-center-coding`, a multi-field data-entry form); on `PAID`/`REJECTED` the terminal `result`; on `FAILED` an `error`
- `submit_input` → routes by key: `approval` → `ApproveInvoice`/`RejectInvoice`; `cost-center-coding` → `SubmitCostCenter(content)` (or `RejectInvoice` on decline/cancel)
- `cancel` → cancels the workflow

**Task Lifecycle (FastMCP tasks — polling for status, server-push elicitation for input):**
```
Client                          MCP Server                      Temporal
  |-- tools/call (task=True) --->|                               |
  |   process_invoice            |-- start_workflow ------------>|  (taskId = workflow_id)
  |<-- task stub (_meta taskId)--|  (status: working)            |
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: working ----------|<-- PENDING-VALIDATION --------|
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: input_required ---|<-- PENDING-APPROVAL ----------|
  |-- tasks/result(taskId) ----->|-- get_state: input_required ->|
  |<== elicitation/create =======|  (server PUSH; x-task-id +    |
  |    {approve/reject schema}   |   x-request-key=approval)     |
  |== ElicitResult(approve) ====>|-- signal ApproveInvoice ----->|
  |   (client cancels tasks/result after answering; resumes polling)
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: working ----------|<-- RECONCILING (slow) --------|   (client polls several times)
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |<-- status: input_required ---|<-- PENDING-COST-CENTER -------|   (2nd gate, large invoices only)
  |-- tasks/result(taskId) ----->|-- get_state: input_required ->|
  |<== elicitation/create =======|  (x-request-key=cost-center-coding, multi-field form)
  |== ElicitResult(cost ctr) ===>|-- signal SubmitCostCenter --->|
  |-- tasks/get(taskId) -------->|-- query GetInvoiceStatus ---->|
  |-- tasks/result(taskId) ----->|-- wait_result --------------->|
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

A reusable, Temporal-backed implementation of the MCP task protocol built on **FastMCP's task support** (SEP-1686, `fastmcp==2.14.3`). The invoice app is its first consumer.

**Usage guide for building your own tasks server/client with this package:** [`mcp_tasks_temporal/README.md`](mcp_tasks_temporal/README.md).

- **`server.py`** — `register_tasks_extension(mcp, backend, *, task_tools=None)`: overwrites FastMCP's low-level task handlers (`mcp._mcp_server.request_handlers`) with backend-driven ones. `tools/call` for a task tool → `backend.start` → a task stub (`_meta` taskId + status); `tasks/get` → `backend.get_state` → status-only `GetTaskResult`; `tasks/result` → if terminal return the result, else **push** the single pending inputRequest as `elicitation/create` (with `x-task-id` + `x-request-key` injected into `requestedSchema`), `backend.submit_input` the answer, then `backend.wait_result`; `tasks/cancel` → `backend.cancel`.
- **`backend.py`** — `TaskBackend` protocol (`start`/`get_state`/`submit_input`/`wait_result`/`cancel`) + `TaskState` + plain `InputRequest`/`InputResponse` (the MCP-facing task state a backend maps its workflow state onto).
- **`client/`** — the durable client: `workflows.TaskTrackerWorkflow` (poll status → on `input_required` run `handle_elicitation` → its handler signals `elicitation_received` and reads `user_decision` → terminal), `activities.MCPActivities` (`start_task`/`poll_task`/`handle_elicitation`/`get_task_result` over a shared FastMCP client, plus the bound `_elicitation_handler`), `session.py` (FastMCP `Client` with the elicitation handler), `models.py` (sandbox-safe dataclasses + activity-name string constants).
- **`plugin.py`** — `MCPTasksClientPlugin(config, temporal_client)`: a Temporal `SimplePlugin` bundling the workflow + activities + the FastMCP session lifecycle (via `run_context`). One `plugins=[...]` entry adopts the whole durable client. The `temporal_client` lets the elicitation handler signal/query trackers from FastMCP's receive loop.

### Client Application (`invoice_processing_mcp/client/`)

The invoice consumer of the durable client; the reusable parts live in `mcp_tasks_temporal`.

- **`worker.py`** — reads the Claude Desktop-format config (the dict FastMCP consumes), and runs `Worker(client, task_queue=models.TASK_QUEUE, workflows=[PurchaseOrderWorkflow], activities=[<back-office>], plugins=[MCPTasksClientPlugin(config, client)])`. The plugin's `TaskTrackerWorkflow` + wire activities are **merged** with these explicit lists (do **not** re-list `TaskTrackerWorkflow`).
- **`purchase_order_workflow.py`** — `PurchaseOrderWorkflow`, the parent orchestrator: records goods receipt, starts `process_invoice` as a **child `TaskTrackerWorkflow`** (`workflow.start_child_workflow`, child id `task-tracker-{workflow.uuid4()}`), then `asyncio.gather`s the child handle with the back-office work so they run **concurrently**. Query `get_progress` returns `{po_id, steps_done, payment_status, payment_workflow_id}` (the child task id). The MCP task is one step of a larger durable business process, not the whole thing.
- **`backoffice_activities.py`** — `mcp`-free activities (`record_goods_receipt`, `update_inventory`, `notify_requester`, `close_po`) the parent runs alongside payment; per-step delay via `BACKOFFICE_DELAY_SECONDS` (default 2s). Kept `mcp`-free so the sandbox re-import of the parent stays clean; referenced by function object.
- **`ui.py`** — Temporal-only CLI. `submit <file>` starts a `PurchaseOrderWorkflow`; `list` shows running POs with `get_progress` and renders any pending `inputRequests` from the child trackers (still discovered by `WorkflowType = "TaskTrackerWorkflow"`), then signals `user_decision`. `_prompt_for` renders arbitrary multi-field schemas (optional fields skippable, enums validated).
- **`gui.py`** — Temporal-only NiceGUI status board. Lists **all** `PurchaseOrderWorkflow`s (running + closed, newest first) via `list_workflows`; per PO, resolves the child task id from `get_progress` and shows the child's live `get_status`; auto-refreshes every `--refresh-seconds` (default 2s ≈ the server poll interval). Clicking an `input_required` status renders that task's pending question (from the child's `get_pending_input`) in a pane above the list; **Submit** signals `user_decision` (reject by choosing `reject` in the form), **Close** dismisses without answering. A submit pane (**Submit simple/large**) starts a PO from a sample with a randomized `invoice_id` + ±10% line amounts. Temporal-facing helpers (`fetch_po_rows`, `build_responses`, `submit_decision`, `randomize_invoice`, `start_po`) are factored out and unit-tested (`tests/test_gui.py`); rendering is verified manually.

**Server-push elicitation + the reader-release workaround.** Because the server pushes
`elicitation/create` during `tasks/result`, the client uses `_elicitation_handler` + `_active_elicitations`
(task_id → tracker workflow id) + `x-task-id`/`x-request-key` routing. The handler checks once for a
decision and **raises if none is ready**, so `handle_elicitation` fails and Temporal retries after
backoff — briefly releasing the shared MCP reader rather than holding it (the documented concurrency
caveat; see `docs/research/tasks-protocol-gaps.md`, SEP-2322).

### Legacy MCP CLI Client — REMOVED
The old LLM-driven CLI (formerly `async_mcp/mcp_client/`, OpenAI Responses API) was **deleted** earlier and not restored (no `openai` dependency). Some historical design/plan docs under `docs/` still reference it for context.

## Key Patterns

- **Tasks come from FastMCP; we plug Temporal in via a backend.** `register_tasks_extension(mcp, backend, task_tools=...)` overwrites FastMCP's Docket-based task handlers on the low-level server with backend-driven ones (this package is the reusable Temporal seam). The MCP task id is the value our backend returns in the tool-call `_meta` (= the workflow id).
- **The `TaskBackend` shape is anticipatory of native v2.** `TaskState.input_requests` is the keyed `{key: InputRequest(method="elicitation/create", params={message, requestedSchema})}` map surfaced on `input_required` — i.e. the native v2 Tasks `inputRequests` structure, produced by `InvoiceTaskBackend.get_state` and consumed as-is by the UI (`get_pending_input`/`user_decision`). Only the *transport* is v1: the generic `tasks/result` handler **pushes** that InputRequest via `elicit_form` (+ `x-task-id`/`x-request-key`) instead of returning it in `tasks/get`. Re-targeting native v2 is therefore a transport swap, not a backend/UI rewrite — see `docs/decisions/003-revert-to-v1-fastmcp-tasks.md` → "Forward compatibility".
- **Server-side: workflow ID = MCP task ID** (no lookup table). **Client-side:** `TaskTrackerWorkflow` ID is `task-tracker-{uuid}` (minted by `PurchaseOrderWorkflow` via `workflow.uuid4()` and exposed through its `get_progress`), distinct; the MCP task ID lives in workflow state. Temporal `list_workflows` is the durable task registry.
- **HITL via server-push elicitation**: `PENDING-APPROVAL`/`PENDING-COST-CENTER` → `tasks/get` reports `input_required`; the client then opens `tasks/result`, the server **pushes** an `elicitation/create` carrying the inputRequest (with `x-task-id` + `x-request-key`), and the client answers with an `ElicitResult`. **Multi-round HITL** uses distinct keys per round (`approval`, then `cost-center-coding`); `TaskTrackerWorkflow` resets `_decision`/`_pending_input` each round so it re-enters `input_required` cleanly. The client cancels `tasks/result` after each answer and resumes polling.
- **Parent orchestrator + child task**: `PurchaseOrderWorkflow` runs the MCP task as a child `TaskTrackerWorkflow` while doing other durable work concurrently — the durable-orchestration value story. The plugin's workflows/activities **merge** with the explicit `workflows=`/`activities=` on the same `Worker` (append; do not re-list `TaskTrackerWorkflow`). Verified on `temporalio` 1.29.
- **Sandbox cleanliness**: `TaskTrackerWorkflow` imports only `mcp_tasks_temporal.client.models` (pure dataclasses) and references activities by string name, so `fastmcp`/`mcp` never load in the workflow sandbox. `PurchaseOrderWorkflow` imports its back-office activities under `with workflow.unsafe.imports_passed_through():` and those activities are `mcp`-free. Package `__init__` files are kept import-light for the same reason. Worker (and the sandboxed tests) pass `SandboxedWorkflowRunner(restrictions=...with_passthrough_modules("beartype"))` — required because `fastmcp` imports `beartype`, which hits a circular import if re-imported under the sandbox.
- Invoice JSON: `{"invoice_id": str, "customer": str, "lines": [{"description": str, "amount": number, "due_date": ISO8601}]}`
- Temporal address via `TEMPORAL_ADDRESS` (default `localhost:7233`); client config uses Claude Desktop format `{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}}}`.
- **Testing**: the generic server + invoice backend are tested end-to-end over **FastMCP's in-memory transport** (`Client(mcp, elicitation_handler=...)`), driving the server-push elicitation including a two-gate flow; `TaskTrackerWorkflow` and `PurchaseOrderWorkflow` run under `WorkflowEnvironment.start_time_skipping()` (the PO test asserts back-office work completes *while* payment is pending, then drives the child). The time-skipping test server has **no visibility API**, so the PO test reads the child id from the parent's `get_progress` rather than querying by type. The real-workflow E2E is the manual demo — `InvoiceWorkflow`'s 5-day approval timer makes the time-skipping env auto-approve, so it isn't in the suite.
- `fastmcp[tasks]==2.14.3` is pinned (2.14.7 dropped the `tasks` extra; 3.x changed the task wire protocol); it pulls `mcp` 1.x. `temporalio>=1.19` for the Plugins API; `nicegui` for the GUI status board.
