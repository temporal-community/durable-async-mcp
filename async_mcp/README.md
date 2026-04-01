# Async MCP Server (Tasks + Elicitation)

This MCP server implementation uses **MCP Tasks** (SEP-1686) for async long-running operations and **MCP Elicitation** for human-in-the-loop approval flows, all backed by Temporal durable workflows.

Custom task protocol handlers (`temporal_task_handlers.py`) replace FastMCP's default Docket/Redis-based task layer, mapping the MCP task lifecycle directly to Temporal workflows — the Temporal workflow ID *is* the MCP task ID.

Since no existing MCP clients (Claude Desktop, Cursor, etc.) support the Tasks protocol yet, this directory includes its own CLI client (`mcp_client/`) that implements the full task lifecycle.

## Running the Demo

**Prerequisites:** Temporal server and worker must be running (see [root README](../README.md)).

### Quick boot (requires tmux)

```bash
./async_mcp/boot-demo.sh
```

This starts the Temporal server and worker in a tmux session.

### Run the CLI client

```bash
export OPENAI_API_KEY=sk-...
python -m async_mcp.mcp_client [--config async_mcp/client_config.json] [--model gpt-4o]
```

### Try it out

```
You: Process this invoice: {"invoice_id": "INV-100", "customer": "ACME Corp", "lines": [{"description": "Widget A", "amount": 100, "due_date": "2024-06-30T00:00:00Z"}]}
You: What are the open invoices?
You: Approve that invoice
You: exit
```

### What happens

1. **Task starts immediately** -- The LLM calls `process_invoice`, the client starts a Temporal workflow via the MCP task protocol, and begins polling for status
2. **Approval prompt** -- When validation completes, the task transitions to `input_required`. The client calls `tasks/result`, which triggers an elicitation request asking you to approve or reject
3. **You decide** -- Approve or reject the invoice in your terminal
4. **Payments process** -- If approved, each line item is paid via the payment gateway
5. **Results returned** -- Final status (PAID, FAILED, or REJECTED) is returned to the LLM

## Available Tools

**Server tools** (discovered from the MCP server):
- **`process_invoice`** (task-enabled) -- Starts a new invoice workflow. Returns a task ID immediately; the client polls and handles elicitation.

**Client-side tools** (defined in the client, mapped to MCP protocol operations):
- **`list_tasks`** -- Lists active invoice workflows via `tasks/list`.
- **`resume_task`** -- Resumes an existing task by ID -- polls `tasks/get`, then calls `tasks/result` to trigger approval elicitation.

## Architecture

### MCP Server (`server.py`)

Exposes `process_invoice` as a task-enabled tool via FastMCP. When called with task metadata, starts a Temporal workflow and returns a task ID immediately.

### Task Handlers (`temporal_task_handlers.py`)

Custom MCP task protocol handlers that replace FastMCP's Docket/Redis layer:

- **`register_temporal_task_handlers(mcp)`** -- Entry point, overwrites 5 request handlers on FastMCP's low-level server
- **`handle_tasks_get`** -- Queries `GetInvoiceStatus` on the Temporal workflow, maps to MCP task state
- **`handle_tasks_result`** -- For terminal states: returns `CallToolResult`. For `PENDING-APPROVAL`: triggers elicitation, signals workflow, awaits result
- **`handle_tasks_list`** -- Lists active invoice workflows via Temporal's `list_workflows`
- **`handle_tasks_cancel`** -- Cancels a running workflow via Temporal's cancel API

### CLI Client (`mcp_client/`)

An interactive terminal client that uses an LLM (OpenAI Responses API) to drive MCP tool calls. Supports the full Tasks protocol including elicitation.

- **`mcp_client/main.py`** -- Entry point: config loading, MCP connection, chat loop, elicitation handler
- **`mcp_client/llm.py`** -- OpenAI integration: schema conversion, conversation state, LLM calls

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
uv run pytest async_mcp/tests/
```
