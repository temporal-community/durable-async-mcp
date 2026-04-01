# Invoice Processing with Temporal + MCP

Demonstrates how to integrate Temporal durable workflows with the Model Context Protocol (MCP). An invoice processing workflow serves as the example business logic, with different MCP server implementations showing different integration patterns.

## Repository Structure

```
bizservice/          Temporal workflows, activities, worker, and CLI
async_mcp/           MCP server using Tasks (SEP-1686) + Elicitation
  mcp_client/        CLI client for the async MCP server
samples/             Sample invoice JSON files
docs/                Design docs, research, and plans
```

### [`bizservice/`](bizservice/README.md)
The core business logic: an invoice processing workflow with validation, human approval, and parallel line-item payments. Independent of any MCP implementation. Includes a CLI for interacting with workflows directly via Temporal.

### [`async_mcp/`](async_mcp/README.md)
An MCP server that uses **Tasks** for async execution and **Elicitation** for human-in-the-loop approvals. Custom task handlers map the MCP task lifecycle directly to Temporal workflows (workflow ID = task ID). Includes its own CLI client since no existing MCP clients support the Tasks protocol yet.

## Prerequisites

- Python 3.10+
- `uv` (curl -LsSf https://astral.sh/uv/install.sh | sh)
- Temporal [Local Setup Guide](https://learn.temporal.io/getting_started/)
- An OpenAI API key (for the async MCP CLI client)

## Setup

```bash
git clone https://github.com/Aslan11/temporal-invoice-mcp.git
cd temporal-invoice-mcp
uv venv
source .venv/bin/activate
uv pip install -e .
```

## Quick Start

```bash
# Terminal 1: Start Temporal server
temporal server start-dev

# Terminal 2: Start the worker
python -m bizservice.worker

# Terminal 3: Run the async MCP client
export OPENAI_API_KEY=sk-...
python -m async_mcp.mcp_client --config async_mcp/client_config.json
```

Or use the helper script (requires tmux) to start the server and worker, then run the client manually:

```bash
./async_mcp/boot-demo.sh
```

The sample invoice lives at `samples/invoice_acme.json`. Inspect Temporal Web at `http://localhost:8233`. Kill and restart the worker at any time to observe deterministic replay.

## Running Tests

```bash
uv run pytest async_mcp/tests/
```

## Why This Is Interesting

1. **MCP Tasks for async operations** -- The invoice processing runs as a background task. The client gets a task ID immediately and polls for progress without blocking.

2. **Elicitation for human-in-the-loop** -- When approval is needed, the MCP server requests input via elicitation. No separate "approve" or "reject" tools needed.

3. **Temporal for durability** -- The underlying Temporal workflow survives crashes, can wait days for approval, and provides full execution history via Temporal Web UI.
