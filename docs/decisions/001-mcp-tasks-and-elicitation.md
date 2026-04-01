# ADR-001: Replace separate approve/reject tools with MCP Tasks and Elicitation

## Status

In progress (branch: `mcp-tasks`, uncommitted)

## Date

2026 (exact date unrecorded — retroactive ADR)

## Context

The original MCP server exposed four tools to the client:

- `process_invoice` — start a Temporal workflow and return workflow/run IDs
- `approve_invoice` — signal the workflow to approve
- `reject_invoice` — signal the workflow to reject
- `invoice_status` — query workflow status

This design had several drawbacks:

1. **Orchestration burden on the client.** The AI agent had to know the right sequence: call `process_invoice`, poll `invoice_status` until `PENDING-APPROVAL`, then call `approve_invoice` or `reject_invoice`. If the agent didn't follow this protocol correctly, the workflow would hang at the approval step.

2. **No native async support.** `process_invoice` returned immediately with workflow IDs, but there was no standard mechanism for the client to track progress or know when to act. The agent had to improvise a polling loop.

3. **Human-in-the-loop was indirect.** The human could only approve/reject by asking the agent to call a tool. There was no structured prompt — just the agent deciding when and how to ask.

MCP has since introduced two specifications that address these issues directly:

- **MCP Tasks (SEP-1686)** — a standard for long-running, async tool invocations with progress tracking and state transitions (`working`, `input_required`, `completed`, `failed`).
- **MCP Elicitation** — a mechanism for the server to request structured input from the user mid-task, pausing the task until the user responds.

## Decision

Consolidate the four tools into two:

1. **`process_invoice`** (task-enabled) — a single long-running task that handles the full invoice lifecycle: validation, approval (via elicitation), and payment processing. The task transitions through MCP states that map to Temporal workflow statuses.

2. **`invoice_status`** — retained as a lightweight query tool for cases where the task ID is unavailable (e.g., after TTL expiry or for external audit).

The `approve_invoice` and `reject_invoice` tools are removed entirely. Approval is handled via `ctx.elicit()` inside the running task, which surfaces a structured prompt to the user through the MCP client.

## Consequences

### Positive

- **Simpler client integration.** One tool call kicks off the entire flow. The client uses standard `tasks/get` to monitor progress — no custom polling logic needed.
- **Structured approval UX.** Elicitation presents the user with a clear approve/reject choice, including invoice details and amount, at exactly the right moment.
- **Progress visibility.** The task reports progress (0%, 25%, 50%, 100%) at each stage, giving the client meaningful status updates.
- **Better state mapping.** Temporal workflow states map cleanly to MCP task states (see design doc for the mapping table).

### Negative

- **Requires fastmcp >= 2.14.0.** Older clients without task/elicitation support cannot use this version.
- **Less granular control.** External systems that previously called `approve_invoice` independently now need to go through the MCP task flow or signal the Temporal workflow directly.
- **Unused code.** The `ApprovalDecision` Pydantic model is defined but not currently used — the implementation uses a simple string list for `response_type` instead, for broader client compatibility.

## Alternatives Considered

- **Keep separate tools, add task support to `process_invoice` only.** Rejected because it would still require the client to know which tool to call and when, defeating the purpose of the task abstraction.
- **Use a Pydantic model for elicitation response.** Attempted (`ApprovalDecision` class exists in code) but switched to `response_type=["approve", "reject"]` for simpler client rendering.
