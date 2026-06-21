# The Durable-Client Thesis After MCP's Stateless Redesign

> **Status: current.** Written for the 2026-07-28 redesign; this thesis is realized by the
> Temporal-backed Tasks extension in [`mcp_tasks_temporal/`](../../mcp_tasks_temporal/README.md) — see
> [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md).

A positioning note written 2026-05-31, after reviewing the MCP 2026-07-28 release
candidate. Companion to [`../research/mcp-2026-07-28-spec-impact.md`](../research/mcp-2026-07-28-spec-impact.md),
which catalogs the spec changes; this note argues what they mean for the project's
core thesis — *that durable execution (Temporal) on the client side is valuable.*

## Thesis statement (repositioned)

> MCP's 2026-07-28 redesign makes the **wire** stateless while pushing **durability
> obligations outward onto the participants** — clients must persist task handles or
> orphan in-flight work, and a "task" is defined as a *durable state machine* that the
> protocol names but does not provide. Durable execution is the substrate those edge
> semantics presuppose. It is no longer a workaround for a stateful protocol; it is the
> implementation of the protocol's stateful edges.

This is a stronger and more honest position than the pre-redesign pitch, because it is
*intrinsic* to the stateless design rather than an artifact of protocol plumbing.

## 1. What the redesign did to the old argument

Be honest about this first, because a skeptic will be. A meaningful slice of the original
client worker existed to fight the *protocol*, not to add durable value:

- cancel-and-retry around the blocking `tasks/result` connection,
- the `_elicitation_handler` reader-starvation dance (the 20ms check-once-and-raise),
- the `x-task-id` smuggling to route an out-of-band elicitation callback.

The 2026-07-28 redesign removed the accidental complexity these solved (no blocking
result method; elicitation now folds into the task via `input_required` + `tasks/update`;
no unsolicited server→client requests). If *that* had been the core of the pitch, the
pitch would now be in trouble. The flashiest old demo moment — retry cadence visible in
workflow history solving reader starvation — is gone.

Losing a problem you were solving is fine **if there are other, better problems.** There
are, and they are intrinsic rather than incidental.

## 2. The stateless protocol pushes two durability burdens to the edges

### 2a. Client side: lose the handle, orphan the task

`tasks/list` is removed (verified in [`schema/draft/schema.ts`](https://github.com/modelcontextprotocol/experimental-ext-tasks/blob/main/schema/draft/schema.ts):
the only task methods are `tasks/get`, `tasks/update`, `tasks/cancel`, and
`notifications/tasks` — no `ListTasks`, no `tasks/result`). The `taskId` is handed to the
client exactly once, in the `CreateTaskResult` response to `tools/call`. There is no
server-side enumeration to fall back on.

Consequences if the client does not persist the handle:

- **Orphaned from the protocol, not the world.** The task keeps running server-side; the
  client simply can no longer poll status, retrieve the result, or answer input. The work
  may complete with real side effects nobody is watching.
- **`input_required` + lost handle = permanently stuck.** Input is delivered only via
  `tasks/update` with the taskId. Lose the handle while a task waits on a human decision
  and it sits at `input_required` until TTL, then expires unsatisfied. Worst case for a
  human-in-the-loop flow — and the project's core scenario.
- **Orphaned at birth.** If the `CreateTaskResult` response is dropped after the server
  durably created the task, the client never received the ID. With receiver-generated task
  IDs there is no deterministic retry, and no `tasks/list` to clean up after it (see
  `../research/mcp-tasks-limitations-research.md` §2.6).

The spec's own client guidance is explicit ([extension overview](https://modelcontextprotocol.io/extensions/tasks/overview),
*For MCP clients*, step 5): *"Store task IDs durably so polling can resume after a client
crash or restart,"* reinforced by the *"Why not just block?"* line: *"A task ID is a
durable handle. If the client disconnects or restarts, it can resume polling with the same
ID."* Under 2025-11-25 you had an escape hatch — lose the ID and rediscover via
`tasks/list`. That hatch is gone. Persistence went from a resilience nice-to-have to **the
only client-side recovery mechanism that exists.**

### 2b. Server side: "durable state machine" is aspiration, not a guarantee

The README still defines a task as a *"durable state machine that carries information about
the underlying execution state of a request"* — near-verbatim from November (only "the
request they wrap" → "a request"). So the framing was retained, not dropped.

But the normative text ([`docs/specification/draft/tasks.mdx`](https://github.com/modelcontextprotocol/experimental-ext-tasks/blob/main/docs/specification/draft/tasks.mdx))
makes only one hard durability MUST, and it is narrow:

> "A server **MUST NOT** return `CreateTaskResult` until the task is durably created — that
> is, until a `tasks/get` for the returned `taskId` would resolve."

That guarantees the handle is *queryable the instant you receive the ID*. It says nothing
about the task surviving a server crash later. TTL undercuts even retention:

> "servers **MAY** mark a task as `failed` at any point after the TTL elapses, and
> subsequently delete it."

So the spec is silent on server-outage survival. *"Durable state machine"* is design intent
and connotation; *"durably created"* is the only normative teeth, and it covers
creation-time queryability, not crash survival.

## 3. MCP places semantics at the edges — correcting "just transport"

An earlier draft of the analysis claimed MCP "is just a message contract" and therefore
*cannot* mandate durability. **That was an overreach, and it's retracted here.** MCP is an
application-layer protocol, and such protocols routinely place behavioral obligations on
implementations, not just wire-format rules. The meaning of "task" in MCP is established by
prose MUSTs, by the connotation of "durable," **and by the reference implementation** — in
MCP's ecosystem the SDKs and reference servers carry de-facto normative weight precisely
because the prose is thin.

The decisive evidence: the November 2025 reference implementation backed tasks with
**Docket + Redis** specifically so they would survive server outages. That is the
durability semantics being operationalized — proof "durable state machine" was never idle
prose. This project's server side already took the next step: `temporal_task_handlers.py`
**replaces FastMCP's Docket/Redis layer** with Temporal-backed handlers (workflow-ID =
task-ID).

The point that survives the correction, relocated: **a durability obligation is not
self-executing.** The spec saying "tasks are durable" persists zero bytes; something has to
*be* the durable state machine. The reference answer was Docket+Redis. Temporal is the more
complete answer — it provides not just a durable task *record* but a durable *state
machine*, the thing the spec actually names, plus the signals, queries, timers, and retries
Docket+Redis don't. The obligation being real (the protocol's point) is exactly what makes
the substrate matter (the thesis's point). They are not in tension.

## 4. Stateless wire, stateful edges

Putting §2 and §3 together yields the framing worth carrying into the talk:

**The wire got more stateless while the edges got more durability-obligated — at the same
time.** Removing sessions and `tasks/result` made the protocol stateless on the wire.
Removing `tasks/list` and leaning on "persist task IDs" *increased* what the spec demands of
edge state. MCP is deliberately pushing statefulness *outward* — off the connection, onto
the participants — and then placing durability semantics on those participants on both
sides:

- **Client edge:** persist the handle or orphan the work (no `tasks/list`).
- **Server edge:** be a durable state machine (the definition of "task") with no protocol
  mechanism that provides it.

Durable execution is the substrate both edges presuppose. The same engine supplies both
halves: the handle survives on the client because it lives in `TaskTrackerWorkflow` history;
the execution survives on the server because the task *is* a Temporal workflow.

## 5. Where the thesis is strong, and where it isn't

Honest boundary conditions — the thesis is not "always use Temporal for MCP tasks."

**The skeptic's counterargument (fair):** the stateless poll-based protocol is now simple
enough that a thin async client with a `(task_id, status)` table handles concurrency fine.
For a single task or an ephemeral client, that's correct, and a workflow engine to poll one
handle to completion is over-engineering.

**Where it breaks — and these are the project's actual conditions:**

- **Concurrency** — many in-flight tasks, each an independently stateful, long-lived poller.
- **Long duration + crash recovery** — kill the client mid-wait and resume exactly where it
  was; the DB-table client must hand-build resume, backoff, and timers.
- **Human-in-the-loop** — multi-UI signaling into a long-running wait; no protocol
  equivalent to signals/queries.
- **Multi-step / multi-task orchestration** — fan-out across several MCP tasks (possibly
  across servers), join, compensate on failure.

Each is a thing the thin client hand-builds. The moment it implements "persist task IDs and
resume polling after a crash," it is building a worse Temporal. The DB-table approach is
fine right up until it needs all four — at which point it *is* a workflow engine, badly.

**Adoption caveat:** the thesis is strongest when you **own the client/agent runtime**
(custom agents, enterprise orchestration, your own host). For an off-the-shelf host you
don't control (e.g., Claude Desktop), your workflow lives beside or behind it, and the
client that polls may not be yours to make durable. Name that boundary rather than letting
someone find it.

## 6. The application-logic reframe

The strongest frame is not "workflow that manages the MCP task protocol" but **"workflow for
the agent's own durable, long-running, human-in-the-loop, concurrent process — in which an
MCP task is a single durable step."** The MCP task becomes an activity (or a poll-loop the
workflow owns via durable timers) inside the agent's workflow. The workflow models the
business/agent process; MCP tasks are steps within it. This is more defensible than "we make
a hard protocol tolerable," because it does not depend on the protocol being hard — it
depends on the agent's *own* process being long-running, concurrent, and recoverable, which
it is.

## 7. Demo implications

The old hero moment (retry cadence solving reader starvation) is gone. The honest
replacements demonstrate *intrinsic* value rather than workaround cleverness:

- Kill the client during a multi-day approval wait, restart it, and watch N concurrent tasks
  resume polling from exactly where they were.
- Submit many invoices at once and watch a durable poller per task, each independently
  stateful.
- Multi-UI human approval signaled into a long-running wait.
- Kill the *server* worker mid-execution and watch the task (the Temporal workflow) survive
  and complete — making the spec's "durable state machine" adjective literally true, which a
  naive in-memory-but-spec-compliant server would not.

## Evidence and sourcing caveats

- Methods / removal of `tasks/list` and `tasks/result`: verified against
  `schema/draft/schema.ts` in `experimental-ext-tasks`.
- "durably created" MUST and the TTL MAY: quoted from `docs/specification/draft/tasks.mdx`.
- "durable state machine" definition: `README.md` of `experimental-ext-tasks`.
- Client persistence guidance: `modelcontextprotocol.io/extensions/tasks/overview`.
- All of the above were read during the **RC window** (`main`/`draft`), via the fetch tool.
  The claims that carry the most weight — `tasks/list` removed; the spec does **not** require
  server-outage survival (only "durably created"); elicitation moves to `tasks/update` — are
  worth re-verifying directly against the normative text when the final spec locks on
  2026-07-28 before they go on a slide.
- The Docket/Redis → Temporal lineage is grounded in this repo: see
  `async_mcp/temporal_task_handlers.py` ("replace FastMCP's Docket/Redis layer") and CLAUDE.md.
