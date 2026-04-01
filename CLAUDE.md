# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an invoice processing demo that integrates Temporal workflows with the Model Context Protocol (MCP). It uses MCP Tasks (SEP-1686) for async long-running operations and MCP Elicitation for human-in-the-loop approval flows.

The repo contains a shared business service (`bizservice/`) and multiple MCP server implementations:
- `async_mcp/` — Uses MCP Tasks + Elicitation with custom Temporal-backed task handlers
- `durable_sync_mcp/` — Synchronous tools (no MCP Tasks), designed for Claude Desktop over stdio

## Repository Structure

```
bizservice/              Temporal workflows, activities, worker, CLI
async_mcp/               MCP server using Tasks + Elicitation
  server.py              FastMCP server with task-enabled process_invoice tool
  temporal_task_handlers.py  Custom task handlers replacing Docket/Redis
  mcp_client/            CLI client for the async MCP server
  client_config.json     MCP client config (Claude Desktop format)
  boot-demo.sh           tmux helper to start server + worker
  tests/                 Tests for task handlers and client
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

# Terminal 2: Start the worker
python -m bizservice.worker [--fail-validate] [--fail-payment]

# Terminal 3: Start the MCP CLI client
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
- **`handle_tasks_result`** — For terminal states: returns `CallToolResult`. For `PENDING-APPROVAL`: triggers elicitation, signals workflow, awaits result.
- **`handle_tasks_list`** — Lists active invoice workflows via Temporal's `list_workflows`
- **`handle_tasks_cancel`** — Cancels a running workflow via Temporal's cancel API
- **`_make_wrapped_call_tool`** — Wraps FastMCP's `CallToolRequest` handler to intercept task-augmented `process_invoice` calls

### MCP CLI Client (`async_mcp/mcp_client/`)
An interactive CLI client that uses an LLM (OpenAI Responses API) to drive MCP tool calls. Supports the full Tasks protocol including elicitation for human-in-the-loop approvals.

- **`mcp_client/main.py`** — Entry point: config loading, MCP connection via `fastmcp.Client`, chat loop, elicitation handler, client-side tools. Run with `python -m async_mcp.mcp_client`.
- **`mcp_client/llm.py`** — OpenAI integration: MCP->OpenAI tool schema conversion, conversation state management, LLM API calls, response parsing.
- **`async_mcp/client_config.json`** — Sample config (Claude Desktop format) pointing to `async_mcp/server.py` via stdio.

**Client-side tools** (defined in the client, mapped to MCP protocol operations):
- `list_tasks` -> calls `tasks/list` protocol to list active invoice workflows from Temporal
- `resume_task(task_id)` -> polls `tasks/get` then calls `tasks/result` to resume an existing workflow (triggers elicitation if awaiting approval)

**Key workarounds for FastMCP Client**:
- `ToolTask.status()` caches the first result and never refreshes — use `client.get_task_status()` directly for polling
- `ToolTask.result()` internally calls `wait()` which only watches for terminal states, skipping `input_required` — use `_poll_and_resolve_task()` which polls with `get_task_status()` and then calls `get_task_result()` directly

## Key Patterns

- The `process_invoice` tool uses `task=TaskConfig(mode="required")` — clients must use the task protocol. The actual task execution is handled by custom Temporal-backed handlers (not Docket).
- Workflow ID = MCP task ID — no lookup table needed
- Approval is handled via MCP Elicitation inside the `tasks/result` handler when the workflow is in `PENDING-APPROVAL` state
- Invoice JSON structure: `{"invoice_id": str, "customer": str, "lines": [{"description": str, "amount": number, "due_date": ISO8601}]}`
- Temporal address configurable via `TEMPORAL_ADDRESS` env var (default: `localhost:7233`)
- Tests use `temporalio.testing.WorkflowEnvironment.start_time_skipping()` (embedded in-process Temporal test server)
- The MCP CLI client requires `OPENAI_API_KEY` env var to be set
- Client config uses Claude Desktop format: `{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}}}`
