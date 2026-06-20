# Async MCP Server (Tasks + Elicitation)

This MCP server implementation uses **MCP Tasks** (SEP-1686) for async long-running operations and **MCP Elicitation** for human-in-the-loop approval flows, all backed by Temporal durable workflows.

Custom task protocol handlers (`temporal_task_handlers.py`) replace FastMCP's default Docket/Redis-based task layer, mapping the MCP task lifecycle directly to Temporal workflows — the Temporal workflow ID *is* the MCP task ID.

## Processes Overview

There are **four separate processes** (plus the Temporal server):

| Process | Command | What it does |
|---------|---------|--------------|
| Temporal server | `temporal server start-dev` | Orchestration backbone |
| bizservice worker | `python -m bizservice.worker` | Runs `InvoiceWorkflow` + `PayLineItem` on `invoice-task-queue` |
| MCP server | `python -m async_mcp.server` | FastMCP server; uses a Temporal *client* to start/query/signal invoice workflows |
| client worker | `python -m async_mcp.client_worker.worker` | Runs `TaskTrackerWorkflow` + `MCPActivities` on `client-task-queue`; holds the MCP client connection |
| UI | `python -m async_mcp.client_worker.ui` | Interactive CLI; connects to Temporal only (no MCP) |

The MCP server and client worker each hold a Temporal client but neither is a Temporal worker for the other's workflows — they're fully independent.

## Running the Demo

**Prerequisites:** Start the Temporal server first.

```bash
temporal server start-dev
```

Then start each process in a separate terminal (from the repo root with the venv active):

```bash
# Terminal 1 — bizservice worker (invoice processing)
python -m bizservice.worker

# Terminal 2 — MCP server
python -m async_mcp.server

# Terminal 3 — client-side Temporal worker
python -m async_mcp.client_worker.worker

# Terminal 4 — UI
python -m async_mcp.client_worker.ui
```

### Try it out

In the UI terminal:

```
Invoice Processing Client
Commands: submit <file>, list, quit

> submit samples/invoice1.json
  Started: task-tracker-<uuid>

Approval needed for task task-tracker-<uuid>:
  Approve or reject invoice INV-001 for Acme Corp?
  Decision [approve/reject]: approve
  Decision 'approve' sent.

> list
  task-tracker-<uuid>  Running
```

### What happens

1. **Task starts** — UI starts a `TaskTrackerWorkflow`, which calls `process_invoice` on the MCP server via the client worker. The MCP server starts an `InvoiceWorkflow` and returns a task ID.
2. **Validation** — The bizservice worker validates the invoice. `TaskTrackerWorkflow` polls status via `tasks/get`.
3. **Approval prompt** — When the invoice reaches `PENDING-APPROVAL`, `TaskTrackerWorkflow` runs the `handle_elicitation` activity, which calls `tasks/result`. The MCP server triggers elicitation; the activity signals the workflow with the prompt details.
4. **UI displays prompt** — The UI polls `get_elicitation_details` and presents the approval prompt.
5. **You decide** — UI signals `user_decision` to the workflow. The activity detects the decision via `get_pending_decision`, sends it to the MCP server, and cancels the connection (per MCP spec). `TaskTrackerWorkflow` resumes polling.
6. **Payments process** — The bizservice worker processes line items in parallel.
7. **Result** — `TaskTrackerWorkflow` fetches the final result via `tasks/result` and completes.

### Legacy LLM-driven client

The original client uses an LLM (OpenAI Responses API) to drive the MCP tool calls. It is the non-durable predecessor to the client worker, kept for comparison; it requires `OPENAI_API_KEY` and does not use the client worker:

```bash
export OPENAI_API_KEY=sk-...
python -m async_mcp.mcp_client [--config async_mcp/client_config.json] [--model gpt-4o]
```

## Available Tools

**Server tool** (discovered from the MCP server):
- **`process_invoice`** (task-enabled) — Starts a new invoice workflow. Returns a task ID immediately; clients poll via `tasks/get` and handle elicitation via `tasks/result`.

## Architecture

### MCP Server (`server.py`)

FastMCP server that exposes `process_invoice` as a task-enabled tool. When called with task metadata, starts a Temporal `InvoiceWorkflow` and returns a task ID immediately. Not a Temporal worker — only uses a Temporal client.

### Task Handlers (`temporal_task_handlers.py`)

Custom MCP task protocol handlers that replace FastMCP's Docket/Redis layer:

- **`register_temporal_task_handlers(mcp)`** — Entry point, overwrites 5 request handlers on FastMCP's low-level server
- **`handle_tasks_get`** — Queries `GetInvoiceStatus` on the Temporal workflow, maps to MCP task state
- **`handle_tasks_result`** — For terminal states: returns `CallToolResult`. For `PENDING-APPROVAL`: triggers elicitation via `ctx.elicit()`, signals workflow, awaits `handle.result()`. The client cancels this connection after elicitation and resumes polling (per MCP spec).
- **`handle_tasks_list`** — Lists active invoice workflows via Temporal's `list_workflows`
- **`handle_tasks_cancel`** — Cancels a running workflow via Temporal's cancel API

### Client Worker (`client_worker/`)

Durable Temporal-based client that manages the full MCP task lifecycle. Runs on a separate `client-task-queue`.

- **`workflows.py`** — `TaskTrackerWorkflow`: one instance per MCP task. Polls status, triggers elicitation activity, retrieves result. Signals: `elicitation_received`, `user_decision`. Queries: `get_elicitation_details`, `get_pending_decision`.
- **`activities.py`** — `MCPActivities`: holds the shared `fastmcp.Client`. Four activities: `start_task`, `poll_task_status`, `handle_elicitation`, `get_task_result`.
- **`worker.py`** — Worker startup. Wires the elicitation handler and starts the Temporal worker.
- **`ui.py`** — Interactive CLI. Connects to Temporal only (no MCP). Type `list` to see running tasks and handle any pending approvals one at a time.

### Legacy CLI Client (`mcp_client/`)

Interactive terminal client that uses an LLM (OpenAI Responses API) to drive MCP tool calls directly. Superseded by `client_worker/` and kept only as a comparison point — it demonstrates the hand-rolled, non-durable polling/elicitation loop that the durable client replaces. Self-contained, with its own inline chat-loop UI (distinct from `client_worker/ui.py`).

- **`mcp_client/main.py`** — Entry point: config loading, MCP connection, chat loop, elicitation handler
- **`mcp_client/llm.py`** — OpenAI integration: schema conversion, conversation state, LLM calls

## Task State Mapping

| Temporal Status      | MCP Task State   |
|---------------------|------------------|
| INITIALIZING        | working          |
| PENDING-VALIDATION  | working          |
| PENDING-APPROVAL    | input_required   |
| APPROVED            | working          |
| PAYING              | working          |
| PAID                | completed        |
| FAILED              | failed           |
| REJECTED            | completed        |

## Running Tests

```bash
python -m pytest async_mcp/tests/
```
