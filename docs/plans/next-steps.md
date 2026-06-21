# Next Steps / Roadmap

Forward-looking work, captured 2026-06-20. **Where we are now:** the v2 Tasks-extension migration is
complete (see [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md) and
[`temporal-tasks-extension.md`](temporal-tasks-extension.md)). The reusable extension lives in
`mcp_tasks_temporal/`; the invoice app (`invoice_processing_mcp/`, `server/` + `client/`) consumes it.
32 tests green on `mcp==2.0.0a2` + `temporalio` 1.29. stdio transport (client spawns the server).

The three items below are the next things to work on, roughly independent.

---

## 1. Turn the client into a generic chat-loop agent (domain-agnostic)

**Goal:** the client becomes an **agent with a simple chat loop**. It should know **nothing** about
invoice processing — all domain knowledge enters at runtime from the loaded MCP server (via tool
discovery and the `inputRequests`/elicitation schemas the server surfaces).

**Scope / what it touches:**
- The durable layer is already generic and stays: `TaskTrackerWorkflow`, `MCPActivities`,
  `MCPTasksClientPlugin`, `client/models.py` (`TaskTrackerInput(tool_name, arguments)`).
- The only invoice-specific client code today is `invoice_processing_mcp/client/ui.py` (it hardcodes
  `tool_name="process_invoice"` and renders an approve/reject prompt). Replace that with a generic
  agent: discover tools via `tools/list`, let an LLM decide when to call task-augmented tools, render
  **arbitrary** `inputRequests` schemas, and feed results back into the conversation.

**Considerations / open questions:**
- This resembles the deleted legacy `mcp_client/` (an OpenAI chat loop) — but now **durable**, on the
  **v2** protocol, and **generic**. Use the latest Claude models (per project guidance), not OpenAI.
- Should the agent/conversation itself be durable (a chat workflow that starts `TaskTrackerWorkflow`s),
  or a thin loop that drives them? Open design choice.
- Generic elicitation rendering: `ui.py`'s `_prompt_for` already renders an arbitrary
  `requestedSchema` — that logic is the seed for the generic renderer.

---

## 2. A richer sample workflow that exercises the client more

**Goal:** today's flow is ~3 steps (validate → approve → pay). Build a workflow (extend
`InvoiceWorkflow` or add a new sample) where the client visibly does more: **poll several times**
(server step takes longer) → **take user input** → **go back to polling** → then **either finish or
ask for more input** (multiple sequential HITL rounds).

**Scope / what it touches:**
- `bizservice/workflows.py` (more states / an artificial longer-running step / a second gate).
- `InvoiceTaskBackend` mapping (`TEMPORAL_TO_MCP_STATE` + `get_state`) to surface the extra states and
  multiple `inputRequests`.

**Considerations / open questions:**
- **Unique input-request keys.** The Tasks spec requires each `inputRequests` key to be unique over a
  task's lifetime. The current backend uses a single fixed `"approval"` key — multiple rounds need
  distinct keys (e.g. `"approval"`, then `"budget-override"`).
- The client `TaskTrackerWorkflow` poll loop already re-enters `input_required` and resets
  `_decision`/`_pending_input` each round, so multi-round HITL should work — verify with the richer
  workflow. (Pairs naturally with item 1: a generic agent + multi-round HITL is a strong demo.)

---

## 3. (Biggest) Submit a PR to FastMCP with a Temporal-backed task implementation

**Goal:** contribute a **Temporal-specific task implementation to FastMCP** — the concrete "we're not
abandoning FastMCP" path (FastMCP currently backs tasks with Docket+Redis; offer Temporal as an
alternative backend).

**Scope / what it touches:**
- Likely a **refactor of `mcp_tasks_temporal`** to align with **FastMCP's programming model** (FastMCP
  3.0 is built around components / providers / transforms). Our current design deliberately bypasses
  FastMCP and registers handlers on the raw lowlevel `Server`; this item reverses that to fit FastMCP's
  task/provider abstraction.

**Considerations / open questions (resolve before starting):**
- **Which task model does FastMCP support?** FastMCP's task support currently targets the *old*
  2025-11-25 / SEP-1686 model (Docket-based). A Temporal backend PR is either (a) a Temporal backend
  for FastMCP's *existing* task abstraction, or (b) gated on FastMCP adopting the **v2 Tasks
  extension**. Check FastMCP 3.x's current task API first.
- Relationship to the `_sdk_compat` upstreaming note in ADR-002: that was about the *raw SDK*
  result-surface gap; this is about *FastMCP's* abstraction — different layer, same "upstream it"
  spirit.
- This is the natural home for the durable-execution story to reach a wider audience (FastMCP users
  get crash-resilient tasks without Redis).

---

## When resuming
Ask to continue and pick an item. Item 1 + item 2 compose well (generic agent + richer multi-round
HITL); item 3 is the larger, more independent effort and needs the FastMCP-API question answered first.
