# ADR-002: Migrate to the MCP Tasks extension (v2) with a reusable Temporal-backed implementation

## Status

Accepted (2026-06-20, branch `temporal-client`). Supersedes [ADR-001](001-mcp-tasks-and-elicitation.md).

## Context

ADR-001 implemented long-running invoice processing on the **2025-11-25 experimental MCP Tasks**
feature (SEP-1686), via FastMCP plus custom Temporal-backed handlers that overwrote FastMCP's
low-level request handlers (`async_mcp/temporal_task_handlers.py`). Approval used **server-initiated
elicitation** (`ctx.elicit()`) inside a **blocking `tasks/result`** call, which forced an
`_elicitation_handler` + `x-task-id` + check-once-and-raise workaround to avoid the MCP Python SDK's
sequential-reader starvation under concurrency.

The next MCP spec (**2026-07-28**) redesigns Tasks: it graduates from a core feature to an opt-in
**extension** (`io.modelcontextprotocol/tasks`). The lifecycle becomes pure polling — `tasks/result`
and `tasks/list` are removed; human input is surfaced as data (`input_required` → `inputRequests` in
`tasks/get`) and answered via `tasks/update`; there are no server-initiated requests. We verified
against the published `mcp==2.0.0a2` alpha that the SDK ships the `extensions` capability container
but **deliberately omits the tasks extension itself** (see
[`docs/research/v2-alpha-spike-findings.md`](../research/v2-alpha-spike-findings.md) and
[`docs/research/mcp-2026-07-28-spec-impact.md`](../research/mcp-2026-07-28-spec-impact.md)).

## Decision

Migrate to the 2026-07-28 Tasks extension by **authoring the extension ourselves** as a reusable,
Temporal-backed library, and rebuild the invoice app as its consumer.

1. **New package `mcp_tasks_temporal/`** — a generic implementation of the tasks extension on the
   `mcp==2.0.0a2` **lowlevel `Server`**: hand-defined wire types, `register_tasks_extension(server,
   backend)`, a `TaskBackend` protocol (apps map their workflow state → MCP `TaskState`), and a
   durable client (`TaskTrackerWorkflow` + activities) distributed as a **Temporal Plugin**
   (`MCPTasksClientPlugin`). See [its README](../../mcp_tasks_temporal/README.md).
2. **Rebuild the app** as `invoice_processing_mcp/` (`server/` + `client/`), an `InvoiceTaskBackend`
   consumer. Workflow ID = task ID.
3. **Transport: stdio** — the client worker spawns the server subprocess. (HTTP, for an independent
   long-lived server, is a documented future option, not adopted now.)
4. **`tools/call` result seam (`_sdk_compat`).** The alpha validates `tools/call` results against a
   closed union with no task variant, so a bare `CreateTaskResult` is rejected. A narrow, guard-tested
   shim patches the two validation functions. This is the **one core-SDK gap to upstream** (widen the
   result surface, or add a result-type registration hook), not throwaway code.
5. **Drop FastMCP and OpenAI from the live path** and delete the superseded code
   (`temporal_task_handlers.py`, the bespoke `client_worker` internals, the legacy `mcp_client/`).

## Consequences

### Positive
- The blocking `tasks/result`, server-initiated elicitation, the sequential-reader concurrency
  workaround, `x-task-id` routing, and `tasks/list` are all **gone**; HITL is pull-based and simpler.
- The tasks implementation is **generic and reusable** (any task-augmented MCP server/client), and
  the durable client is adopted with one `plugins=[...]` entry.
- Code is already in its final spec shape; only the `_sdk_compat` seam is provisional.

### Negative / trade-offs
- We carry a monkeypatch seam until the SDK closes the `tools/call` result-surface gap upstream.
- Built against an **alpha** SDK (`mcp==2.0.0a2`); APIs may shift before stable (guard-tested).
- `durable_sync_mcp/` (FastMCP, `mcp.server.fastmcp` removed in v2) is **not migrated** and won't
  import in the v2 venv.

## Note on FastMCP

This is **not a permanent move away from FastMCP.** The Temporal-backed extension on the lowlevel
SDK is the *current experiment / direction* for the tasks work; FastMCP remains a viable option (and
`durable_sync_mcp/` still uses it). If FastMCP gains native v2 tasks-extension support, revisiting it
is open.

## Alternatives considered
- **Wait for the SDK/FastMCP to ship tasks.** Rejected: nothing consumable exists (the `ext-tasks`
  repo is spec-only; the alpha omits tasks); authoring it is the deliverable.
- **`structuredContent` carrier instead of the bare `CreateTaskResult`.** Rejected: a private
  side-channel with no interop value; the patch yields the real wire shape (see spike findings).
- **HTTP transport now.** Deferred: stdio is simpler for the demo; HTTP is the path if/when an
  independent server process is wanted.
