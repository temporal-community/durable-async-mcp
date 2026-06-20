# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an invoice processing demo that integrates Temporal workflows with the Model Context Protocol (MCP). It uses MCP Tasks (SEP-1686) for async long-running operations and MCP Elicitation for human-in-the-loop approval flows.

The repo contains a shared business service (`bizservice/`) and multiple MCP server implementations:
- `async_mcp/` — Uses MCP Tasks + Elicitation with custom Temporal-backed task handlers
- `durable_sync_mcp/` — Synchronous tools (no MCP Tasks), designed for Claude Desktop over stdio

The client side of the async MCP integration is in `async_mcp/client_worker/` — a durable Temporal-based client that replaces hand-rolled polling loops. A separate `async_mcp/mcp_client/` is the older LLM-driven client (OpenAI Responses API).

## Repository Structure

```
bizservice/              Temporal workflows, activities, worker, CLI
async_mcp/               MCP server using Tasks + Elicitation
  server.py              FastMCP server with task-enabled process_invoice tool
  temporal_task_handlers.py  Custom task handlers replacing Docket/Redis
  client_worker/         Durable Temporal-based client (worker + UI)
    workflows.py         TaskTrackerWorkflow — one per in-flight MCP task
    activities.py        MCPActivities — drives MCP task protocol
    worker.py            Worker startup (runs TaskTrackerWorkflow + MCPActivities)
    ui.py                Interactive CLI UI (connects to Temporal only, no MCP)
    models.py            Shared dataclasses (ElicitationDetails, TaskTrackerInput)
  mcp_client/            Legacy LLM-driven CLI client (OpenAI Responses API)
  client_config.json     MCP client config (Claude Desktop format)
  boot-demo.sh           tmux helper to start server + worker
  tests/                 Tests for task handlers, activities, and workflows
durable_sync_mcp/        MCP server using synchronous tools (no Tasks)
  server.py              FastMCP server with individual tools for Claude Desktop
  claude_desktop_config.json  Sample Claude Desktop config
  README.md              Setup and usage instructions
samples/                 Sample invoice JSON files
docs/                    Design docs, research, and plans
```

## Commands

### Setup
```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

### Running the Demo
```bash
# Terminal 1: Start Temporal server
temporal server start-dev

# Terminal 2: Start the bizservice worker (processes invoices)
python -m bizservice.worker [--fail-validate] [--fail-payment]

# Terminal 3: Start the async MCP server
python -m async_mcp.server

# Terminal 4: Start the client-side Temporal worker (manages MCP task lifecycle)
python -m async_mcp.client_worker.worker [--config async_mcp/client_config.json]

# Terminal 5: Start the UI (connects to Temporal only, no MCP)
python -m async_mcp.client_worker.ui
# or: python -m async_mcp.client_worker  (defaults to UI)

# Legacy LLM-driven client (requires OPENAI_API_KEY)
python -m async_mcp.mcp_client [--config async_mcp/client_config.json] [--model gpt-4o]

# Or use the helper script (requires tmux)
./async_mcp/boot-demo.sh
```

### Running Tests
```bash
uv run pytest async_mcp/tests/
uv run pytest async_mcp/tests/test_task_handlers.py::TestClassName::test_name  # single test
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
- **InvoiceWorkflow** — Main orchestrator. Validates invoice, waits for approval signal (up to 5 days), processes line items in parallel via child workflows. Status: INITIALIZING -> PENDING-VALIDATION -> PENDING-APPROVAL -> APPROVED/REJECTED -> PAYING -> PAID/FAILED. Signals: `ApproveInvoice`, `RejectInvoice`. Queries: `GetInvoiceStatus`, `GetInvoiceData`.
- **PayLineItem** — Child workflow that waits until due date, then calls payment gateway with retry policy (3 attempts, non-retryable for INSUFFICIENT_FUNDS). Signal: `ForcePayment` skips the due-date timer and pays immediately.

### Activities (`bizservice/activities.py`)
- `validate_against_erp` — Validates invoice (30% random failure, or forced via `FAIL_VALIDATE=true`, disabled via `NO_FAIL_VALIDATE=true`)
- `payment_gateway` — Processes payment (10% INSUFFICIENT_FUNDS, 30% retryable failure, or forced via `FAIL_PAYMENT=true`, disabled via `NO_FAIL_PAYMENT=true`)

### Worker (`bizservice/worker.py`)
Connects to Temporal and polls `invoice-task-queue` for both workflows and activities. Supports `--fail-validate` and `--fail-payment` flags to force failures for testing.

### Async MCP Server (`async_mcp/server.py`)

**Tools:**
- `process_invoice` (task-enabled) — Starts a Temporal workflow and returns immediately with a task ID. The client polls `tasks/get` for status; calls `tasks/result` when `input_required` (to trigger elicitation) or `completed`/`failed`.

**Task Lifecycle:**
```
Client                          MCP Server                    Temporal
  |                               |                            |
  |-- tools/call (task meta) ---->|                            |
  |  (process_invoice)            |-- start_workflow --------->|
  |<-- CallToolResult ------------|  (taskId = workflow_id)    |
  |   (taskId, status:working)    |                            |
  |                               |                            |
  |-- tasks/get(taskId) --------->|-- query GetInvoiceStatus ->|
  |<-- status:working ------------|<-- PENDING-VALIDATION -----|
  |                               |                            |
  |-- tasks/get(taskId) --------->|-- query GetInvoiceStatus ->|
  |<-- status:input_required -----|<-- PENDING-APPROVAL -------|
  |                               |                            |
  |-- tasks/result(taskId) ------>|                            |
  |<-- elicitation: approve? -----|  (ctx.elicit within        |
  |-- user responds: approve ---->|   tasks/result handler)    |
  |                               |-- signal ApproveInvoice -->|
  |                               |-- handle.result() -------->|
  |                               |<-- "PAID" -----------------|
  |<-- CallToolResult ------------|                            |
  |   (status: PAID)              |                            |
```

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

### Task Handlers (`async_mcp/temporal_task_handlers.py`)

Custom MCP task protocol handlers that replace FastMCP's Docket/Redis layer. The Temporal workflow ID *is* the MCP task ID (1:1 mapping, no lookup table).

- **`register_temporal_task_handlers(mcp)`** — Entry point, overwrites 5 request handlers on FastMCP's low-level server
- **`handle_tasks_get`** — Queries `GetInvoiceStatus` on the Temporal workflow, maps to MCP task state
- **`handle_tasks_result`** — For terminal states: returns `CallToolResult`. For `PENDING-APPROVAL`: triggers elicitation, signals workflow, blocks on `handle.result()` until terminal (per MCP spec Result Retrieval #3). The client cancels this request after elicitation and resumes polling; the server coroutine runs to completion with its response discarded.
- **`handle_tasks_list`** — Lists active invoice workflows via Temporal's `list_workflows`
- **`handle_tasks_cancel`** — Cancels a running workflow via Temporal's cancel API
- **`_make_wrapped_call_tool`** — Wraps FastMCP's `CallToolRequest` handler to intercept task-augmented `process_invoice` calls

### Client-Side Temporal Worker (`async_mcp/client_worker/`)

Durable Temporal-based client that manages the full MCP task lifecycle. Replaces hand-rolled polling loops with a `TaskTrackerWorkflow` per in-flight task. The UI is a separate process — it connects to Temporal only (no MCP).

**`TaskTrackerWorkflow` (`workflows.py`)**
- One instance per MCP task. Polls task status, handles elicitation, retrieves final result.
- Signals: `elicitation_received(details)` (from activity), `user_decision(decision)` (from UI)
- Queries: `get_elicitation_details()` (for UI), `get_pending_decision()` (for activity)
- Workflow ID format: `task-tracker-{uuid}`. Task queue: `client-task-queue`.

**`MCPActivities` (`activities.py`)**
- Shares one `fastmcp.Client` connection for the lifetime of the worker.
- `start_task(invoice_json)` — calls `process_invoice` tool with `task=True`, returns task ID (`task.task_id`)
- `poll_task_status(task_id)` — calls `client.get_task_status(task_id)`, returns `status.status` string
- `handle_elicitation(task_id)` — registers `task_id → workflow_id` in `_active_elicitations`, runs `get_task_result` in a background `asyncio.create_task`, and cancels it per MCP spec once the elicitation event fires. Returns `"elicitation_handled"` or `"completed"`.
- `get_task_result(task_id)` — fetches and returns the final `CallToolResult` text
- `_elicitation_handler(message, response_type, params, context)` — registered with `fastmcp.Client` at construction. Reads `x-task-id` from `params.requestedSchema` to look up the target `TaskTrackerWorkflow` in `_active_elicitations`. Signals the workflow with elicitation details, then checks `get_pending_decision` **once** and either returns `ElicitResult` or raises. Does NOT poll — see below.

**Elicitation routing**: `_active_elicitations: dict[str, str]` maps `mcp_task_id → TaskTrackerWorkflow_id`. The server embeds `x-task-id` in the `requestedSchema` (set in `handle_tasks_result` via `ctx.session.elicit_form()`). `_elicitation_handler` reads it back to route without needing `activity.info()` (which is unavailable in FastMCP's dispatch task).

**Elicitation + sequential reader**: The MCP Python SDK (`modelcontextprotocol/python-sdk`, `mcp/shared/session.py`) dispatches `elicitation/create` inline with `await _received_request(responder)` — the reader cannot process other messages while the handler runs. This is an SDK implementation choice, not a spec requirement. With N concurrent tasks all calling `tasks/result`, a handler that polls for minutes would starve `start_task` and other activities. Fix: the handler checks once (~20ms) and raises if no decision, releasing the reader. Temporal retries `handle_elicitation` (capped at 10s backoff) — brief turns on the reader rather than holding it. Elicitation predates Tasks and the SDK was never updated for concurrent use.

**`ui.py`** — Interactive CLI. Connects to Temporal only; no `fastmcp.Client`. Commands: `submit <file>` starts a new task, `list` shows running tasks and surfaces any pending elicitations one at a time, `quit` exits. Elicitation check runs only on `list` — not automatically — so the user can submit new invoices freely. Key operations: `start_workflow(TaskTrackerWorkflow.run, ...)`, `handle.query(get_elicitation_details)`, `handle.signal(user_decision, decision)`.

### Legacy MCP CLI Client (`async_mcp/mcp_client/`)
Older interactive CLI client that uses an LLM (OpenAI Responses API) to drive MCP tool calls. Requires `OPENAI_API_KEY`. Superseded by `client_worker/` (which adds durability) but kept as a working comparison point — it shows the hand-rolled, non-durable approach the client worker replaces. It is fully self-contained: it has its *own* inline chat-loop UI and elicitation prompts in `main.py` (unrelated to `client_worker/ui.py`, which belongs to the durable client). Nothing in the runtime path imports it.

- **`mcp_client/main.py`** — Entry point: config loading, MCP connection via `fastmcp.Client`, chat loop, elicitation handler, client-side tools. Run with `python -m async_mcp.mcp_client`.
- **`mcp_client/llm.py`** — OpenAI integration: MCP->OpenAI tool schema conversion, conversation state management, LLM API calls, response parsing.
- **`async_mcp/client_config.json`** — Sample config (Claude Desktop format) pointing to `async_mcp/server.py` via stdio.

**Key workarounds for FastMCP Client**:
- `ToolTask.status()` caches the first result and never refreshes — use `client.get_task_status()` directly for polling
- `ToolTask.result()` internally calls `wait()` which only watches for terminal states, skipping `input_required` — use `_poll_and_resolve_task()` which polls with `get_task_status()` and then calls `get_task_result()` directly

## Key Patterns

- The `process_invoice` tool uses `task=TaskConfig(mode="required")` — clients must use the task protocol. The actual task execution is handled by custom Temporal-backed handlers (not Docket).
- Server-side: workflow ID = MCP task ID — no lookup table needed
- Client-side: `TaskTrackerWorkflow` ID is `task-tracker-{uuid}` (distinct from the server-side MCP task ID)
- Approval is handled via MCP Elicitation inside the `tasks/result` handler when the workflow is in `PENDING-APPROVAL` state
- FastMCP Client task API: `call_tool(name, args, task=True)` returns `ToolTask` with `.task_id`; `get_task_status(id).status` returns state string; `get_task_result(id)` returns dict validatable as `CallToolResult`
- Elicitation handler signature: `async def handler(message, response_type, params, context)` — pass to `Client(config, elicitation_handler=handler)` or set via `client.set_elicitation_callback(handler)`
- `activity.info().workflow_id` is NOT accessible inside the elicitation callback — FastMCP dispatches it in a new asyncio task that doesn't inherit Temporal's context variables. Use `_active_elicitations` dict lookup instead.
- `_active_elicitations: dict[mcp_task_id, tracker_wf_id]` — set in `handle_elicitation` before calling `get_task_result`; read in `_elicitation_handler` via `x-task-id` from `params.requestedSchema`
- Server embeds routing info in elicitation via `ctx.session.elicit_form(requestedSchema={..., "x-task-id": task_id})` — do NOT use `ctx.elicit()` (it auto-generates schema with no room for custom fields)
- `_make_wrapped_call_tool` always intercepts `process_invoice` — the original `is_task` check via `ctx.experimental.is_task` was broken and has been removed
- Invoice JSON structure: `{"invoice_id": str, "customer": str, "lines": [{"description": str, "amount": number, "due_date": ISO8601}]}`
- Temporal address configurable via `TEMPORAL_ADDRESS` env var (default: `localhost:7233`)
- Tests use `temporalio.testing.WorkflowEnvironment.start_time_skipping()` (embedded in-process Temporal test server)
- Test workers use `SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype", "fastmcp", "mcp"))` to avoid import issues in the sandbox
- `workflow.unsafe.imports_passed_through()` is used in `TaskTrackerWorkflow` to import `MCPActivities` (fastmcp import chain must not be determinism-checked)
- The legacy MCP CLI client requires `OPENAI_API_KEY` env var to be set
- Client config uses Claude Desktop format: `{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}}}`
