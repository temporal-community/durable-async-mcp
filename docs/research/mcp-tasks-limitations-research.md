# MCP Tasks Limitations Research

Research conducted 2026-03-29. Focused on limitations of MCP Tasks (SEP-1686) for agent orchestration, client-driven task control, long-running workflow support, and the mental model gap between "atomic job" and "collaborative workflow."

## 1. Core GitHub Issues and Proposals

### SEP-1686: Tasks (Primary Spec)
- **URL:** https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686
- **Status:** Final, Standards Track
- **Authors:** Surbhi Bansal, Luca Chang (Amazon)
- **Introduced:** MCP spec version 2025-11-25 (experimental)

### Predecessor Issues
- **Issue #982** — [Long running tools / async tools / resumability](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/982) — Master tracking issue listing 8+ PRs
- **SEP-1391** — [Long-Running Operations](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1391) — Earlier tool-specific proposal, rejected in favor of SEP-1686's generic approach
- **Discussion #314** — [Task semantics and multi-turn interactions with tools](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/314) — Early proposal for `progress/get` endpoint and multi-turn hints
- **Discussion #491** — [Asynchronous operations in MCP](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/491) — Competing resource-based vs task-based models
- **Discussion #175, #1227** — Notifications and long-running operations
- **RFC #30 (working-groups)** — [Long Running Task/Job/Async handling](https://github.com/modelcontextprotocol-community/working-groups/issues/30) — Community working group RFC

---

## 2. Key Limitations Identified

### 2.1 No Client-to-Server Signaling During Running Tasks

The protocol provides no mechanism for a client to send arbitrary messages, signals, or data to a running task. Communication is strictly:
- **Client -> Server:** Create task, poll status (`tasks/get`), retrieve result (`tasks/result`), cancel (`tasks/cancel`)
- **Server -> Client:** Status notifications (optional), elicitation requests (only during `input_required`)

There is no equivalent of Temporal's **Signals** — a way to send data into a running workflow. If a server needs input during execution, the only mechanism is:
1. Server transitions task to `input_required`
2. Client calls `tasks/result`
3. Server issues an elicitation request back to the client
4. Client responds
5. Task transitions back to `working`

This is a request-response pattern, not a general-purpose messaging channel. The server cannot receive unsolicited data from the client.

### 2.2 Polling-Only Model (No Push Notifications)

SEP-1686 is explicitly **requestor-driven polling**. The spec says:

> "This SEP supports the former use case [polling], and offers a framework that could extend to support the latter [notifications]."

Push notifications are listed as **Future Work**, not current spec. The optional `notifications/tasks/status` notification exists but:
- Is explicitly optional — "Requestors MUST NOT rely on receiving this notification"
- Cannot replace polling
- Not available on transports without SSE support

**Real-world impact:** Amazon's healthcare customers noted that multi-hour analyses make continuous polling impractical. Enterprise customers are building webhook-based notification systems *outside of MCP* as workarounds.

### 2.3 No Intermediate Results

> "The current task model returns results only upon completion."

Tasks produce a single result at the end. There is no streaming of partial outputs during execution. The Future Work section acknowledges this gap explicitly and proposes building on the task ID association mechanism to support multiple result messages per task lifecycle. Not yet specified.

### 2.4 No Hierarchical Task Relationships (Subtasks)

The spec has no concept of parent/child tasks, task DAGs, or dependent task orchestration. Future Work describes a "nested task execution" model where:
- A parent task could spawn subtasks
- Subtask requirements would be communicated to the client
- A new `tool_required` status might be introduced

This is explicitly deferred. The current model treats each task as independent and flat.

### 2.5 Elicitation Timeout Problem

When a task enters `input_required` and triggers elicitation, the client's timeout counter keeps ticking. From community analysis:

> "While the MCP capability-use request waits for the Elicitation to complete, the Client-side timeout counter continues to tick. Consequently, if the additional input via Elicitation takes too long, the MCP capability-use request will time out."

The server has no way to influence the client's timeout configuration, and the client doesn't know which tools will use elicitation. There is no good solution to this constraint in the current spec.

### 2.6 No Retry / Idempotency Semantics

The protocol defines no retry behavior for failed tasks. Who decides whether to retry — the client or the server? The spec is silent. The SEP-1686 rationale discusses idempotency for task *creation* (using client-generated task IDs for dedup), but says nothing about retrying *execution*.

**Note:** The final spec changed to **receiver-generated** task IDs (vs. the SEP's original client-generated proposal), which *removed* the built-in idempotency mechanism. From the spec:

> "If the response is lost (network failure, timeout), the requestor cannot deterministically retry without potentially creating duplicate tasks."

### 2.7 Agent-Driven Polling is Unreliable

From the SEP motivation, describing the `start_tool` / `get_status` / `get_result` pattern:

> "Agent-driven polling is both unnecessarily expensive and inconsistent — it relies on prompt engineering to steer an agent to poll at all."

And from the Deep Research use case:

> "The agent sometimes runs into issues calling the `get` tool repeatedly — in particular, it calls the tool once before ending its conversation turn, claiming to be 'waiting' before calling the tool again. It cannot resume until receiving a new user message."

Tasks solve this at the *host application* level (the host polls, not the LLM), but the LLM still needs to understand task lifecycle for orchestration decisions.

### 2.8 No Task-to-Tool Association on `tasks/list`

The `tasks/list` response returns task metadata (taskId, status, timestamps) but does NOT include which tool or request type created the task. A client cannot determine what a task represents without maintaining its own mapping. (This is documented in the project's own `docs/research/` notes.)

---

## 3. The "Atomic Job" vs "Collaborative Workflow" Gap

### What MCP Tasks Model Well
- Fire-and-forget background jobs (batch processing, CI/CD runs)
- Status polling for expensive computations
- Single-interaction human-in-the-loop gates via `input_required` + elicitation
- Integration with existing workflow APIs (Step Functions, CI/CD) as thin wrappers

### What MCP Tasks Cannot Model
- **Multi-step collaborative workflows** where the client sends signals/data into a running process at arbitrary points
- **Workflow orchestration** with parallel branches, joins, compensation, and saga patterns
- **Streaming intermediate outputs** during long-running processing
- **Task hierarchies** (parent spawning children, collecting results)
- **Bidirectional conversation** between client and server during task execution (beyond the single `input_required` -> elicitation -> resume cycle)

The fundamental mental model is: a task is a **durable handle to a background job** that the client can poll and eventually collect the result of. It is NOT a **collaborative session** where client and server exchange messages throughout execution.

### Temporal Fills the Gap

From Temporal's blog posts and the project's own architecture:

> "Signals, Queries, and Updates make interacting with Workflows very simple." — Temporal blog

Temporal provides the primitives MCP Tasks lack:
- **Signals:** Client sends data into a running workflow at any time (e.g., approval decisions, configuration changes)
- **Queries:** Client reads workflow state without blocking (richer than `tasks/get` which only returns status)
- **Updates:** Client sends data AND gets a response from the running workflow
- **Child Workflows:** Hierarchical task decomposition
- **Continue-As-New:** Workflows that run indefinitely
- **Durable Timers:** Wait for hours/days without consuming resources

The project's own architecture (mapping workflow ID = task ID) demonstrates the pattern: MCP Tasks provide the protocol-level handle, while Temporal provides the actual orchestration primitives.

---

## 4. Core Model Comparison: Job Submission vs. Interactive Long-Running Process

The MCP Tasks spec and workflow orchestration systems like Temporal embody fundamentally different models for what a "task" is. This section details those differences and their implications.

### 4.1 The Job Submission Model (MCP Tasks)

In this model, a task is a **unit of work submitted by a client and executed by a server**. The client's role after submission is limited: poll for status, provide input when explicitly asked, and collect the final result.

**Control flow:** The server owns execution. The client is a passive observer that occasionally responds to prompts.

```
Client                              Server
  |                                   |
  |-- "do this work" --------------->|  (submit)
  |                                   |  ... server works ...
  |-- "are you done?" -------------->|  (poll)
  |<-- "no, still working" ----------|
  |                                   |  ... server works ...
  |-- "are you done?" -------------->|  (poll)
  |<-- "I need input" --------------|
  |                                   |
  |<-- "approve this?" -------------|  (server-initiated elicitation)
  |-- "yes" ------------------------>|  (client responds)
  |                                   |  ... server works ...
  |-- "are you done?" -------------->|  (poll)
  |<-- "yes, here's the result" ----|  (terminal)
```

**Characteristics:**
- **Unidirectional initiation:** Only the server can request interaction (via elicitation). The client cannot send unsolicited data.
- **Opaque execution:** The client sees status strings (`working`, `input_required`, `completed`) but not structured intermediate state.
- **Single result:** One payload at the end. No streaming, no partial results.
- **Flat structure:** Tasks are independent. No parent/child, no DAGs, no dependencies.
- **Stateless client:** The client needs no memory of what happened during execution — it just collects the result.

**Well-suited for:**
- Batch processing (run a report, generate an analysis)
- CI/CD triggers (start a build, poll for completion)
- Simple approval gates (one human decision point)
- Any operation where the server knows the full plan upfront

### 4.2 The Interactive Long-Running Process Model (e.g., Temporal Workflows)

In this model, a task is a **collaborative, stateful process** that the client (or agent) actively drives. The process exposes rich state, accepts signals at arbitrary points, and the client makes decisions based on intermediate results.

**Control flow:** Shared. The process executes, but the client can observe detailed state and inject decisions at any point.

```
Agent                               Workflow
  |                                   |
  |-- "start processing" ----------->|  (initiate)
  |                                   |  ... workflow works ...
  |-- query: "what's your state?" -->|  (structured state read)
  |<-- {phase: "validated",           |
  |     validation_score: 0.95,       |
  |     flags: ["high_value"]} ------|
  |                                   |
  | (agent reasons about state)       |
  | "high value + high confidence,    |
  |  I'll auto-approve"              |
  |                                   |
  |-- signal: "approve" ------------>|  (client-initiated)
  |                                   |  ... workflow works ...
  |-- query: "what's your state?" -->|
  |<-- {phase: "paying",              |
  |     lines_paid: 2,               |
  |     lines_remaining: 1,          |
  |     failed_lines: []} -----------|
  |                                   |
  | (agent reasons: all going well,   |
  |  I'll wait for completion)        |
  |                                   |  ... workflow completes ...
  |-- "get result" ----------------->|
  |<-- final result -----------------|
```

**Characteristics:**
- **Bidirectional communication:** Both client and server can initiate interaction at any time.
- **Transparent execution:** The client can query structured, detailed intermediate state — not just a status string.
- **Client-driven decisions:** The agent observes state and *chooses* what to do next. The workflow doesn't dictate the interaction pattern.
- **Multiple interaction points:** The client can signal, query, or update the process at any phase, not just when the server asks.
- **Stateful client:** The agent accumulates context across interactions and uses it for reasoning.

**Well-suited for:**
- Multi-step approval workflows where different approvers or criteria apply at different stages
- Agent-orchestrated processes where the LLM needs to reason about intermediate state
- Long-running processes (hours/days) where conditions change and the agent needs to adapt
- Processes with parallel branches where the agent monitors and intervenes selectively

### 4.3 The Critical Differences

| Dimension | Job Submission (MCP Tasks) | Interactive Process (Temporal) |
|---|---|---|
| **Who drives execution?** | Server | Shared — workflow executes, agent steers |
| **Client → Server data** | Only in response to elicitation | Signals and Updates at any time |
| **Intermediate state** | `statusMessage` (opaque string) | Queries return structured data |
| **Interaction pattern** | Server asks, client answers | Either side can initiate |
| **Agent reasoning** | Limited to: poll, respond, collect | Observe state → reason → act → observe |
| **Number of interaction points** | One per `input_required` cycle | Unlimited — signal/query at will |
| **Result model** | Single terminal result | Intermediate + final results |
| **Task relationships** | Flat, independent | Hierarchical (child workflows), DAGs |
| **Failure handling** | `failed` status, client retries externally | Built-in retry policies, compensation, sagas |
| **Duration model** | Implicit TTL, polling intervals | Durable timers, continue-as-new, unbounded |

### 4.4 The Fundamental Tension for Agentic Systems

The job submission model assumes the **server is the expert** — it knows what to do and just needs occasional human input. This works when the server encapsulates a well-defined, deterministic process.

But in agentic systems, the **agent is the expert** (or at least the decision-maker). The agent needs to:

1. **Observe** rich intermediate state to build context
2. **Reason** about what's happening and what should happen next
3. **Act** by sending signals, approvals, configuration changes, or corrections
4. **Adapt** when conditions change mid-process (new information, failures, priority shifts)

This is the OODA loop (Observe-Orient-Decide-Act), and it requires the interactive model. The job submission model short-circuits it: the agent can only observe a status string, and can only act when the server explicitly asks.

### 4.5 What Would Bridge the Gap

For MCP Tasks to support agent-orchestrated workflows, the spec would need:

1. **`tasks/signal` or `tasks/send`** — Client sends structured data to a running task (analogous to Temporal Signals)
2. **`tasks/query`** — Client reads structured intermediate state (richer than `statusMessage`)
3. **Intermediate results** — Server emits structured partial results during execution, not just at terminal state
4. **Client-initiated interaction** — The client can push data without waiting for `input_required`

These are not minor extensions — they represent a shift from "task as job handle" to "task as collaborative session." Whether the MCP spec should make that shift, or whether orchestration should remain in a separate layer (like Temporal), is an open design question.

### 4.6 The Layered Architecture Alternative

Rather than extending MCP Tasks to be a full orchestration protocol, an alternative is to keep MCP Tasks as the **protocol-level handle** and use a workflow engine as the **orchestration layer**:

- MCP provides: tool discovery, task lifecycle, elicitation for simple gates
- Temporal provides: signals, queries, durable state, retries, child workflows, compensation
- The glue: workflow ID = task ID, custom task handlers that delegate to Temporal

This is the architecture this project implements. The tradeoff is that the agent needs awareness of both layers — it uses MCP for tool calls and basic task status, but would need Temporal-aware tools (or MCP tools that wrap Temporal operations) for richer interaction.

---

## 5. Community Workarounds and Alternatives


### Tool-Splitting Convention
Servers split a conceptual operation into `start_X`, `get_X_status`, `get_X_result` tools. Widely used but fragile — relies on LLM prompt engineering to orchestrate correctly.

### Resource-Based Tracking
Proposed in Discussion #491. Operations return a resource URI that clients subscribe to for updates. Rejected as convention-based and ambiguous (hard to distinguish status resources from content resources).

### MCP Agent Mail
Third-party project providing mail-like coordination between agents via inboxes and threads, working around MCP's lack of inter-task communication.

### Webhook-Based Systems Outside MCP
Amazon enterprise customers are building webhook notification systems alongside MCP rather than waiting for push notification support in the spec.

---

## 6. Future Work (Acknowledged in SEP-1686)

The spec explicitly lists these as deferred:

1. **Push Notifications** — Server-initiated notifications for task state changes (webhook-style or persistent channels)
2. **Intermediate Results** — Streaming partial outputs during execution
3. **Nested Task Execution** — Hierarchical parent/child task relationships with `tool_required` status
4. **Idempotency Mechanism** — General-purpose request idempotency (deferred from task-specific solution)

---

## 7. Key Sources

### Primary Spec and Issues
- [SEP-1686: Tasks Specification](https://modelcontextprotocol.io/seps/1686-tasks)
- [SEP-1686 GitHub Issue](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686)
- [Tasks Spec (draft)](https://modelcontextprotocol.io/specification/draft/basic/utilities/tasks)
- [Issue #982: Long running tools / async tools / resumability](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/982)
- [SEP-1391: Long-Running Operations](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1391)
- [Discussion #314: Task semantics and multi-turn interactions](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/314)
- [Discussion #491: Asynchronous operations in MCP](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/491)
- [RFC #30: Long Running Task/Job/Async handling](https://github.com/modelcontextprotocol-community/working-groups/issues/30)

### Blog Posts and Analysis
- [Building long-running interactive MCP tools with Temporal](https://temporal.io/blog/building-long-running-interactive-mcp-tools-temporal)
- [Durable MCP: Using Temporal to give agentic systems superpowers](https://temporal.io/blog/durable-mcp-how-to-give-agentic-systems-superpowers)
- [MCP Async Tasks: Building long-running workflows for AI Agents (WorkOS)](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows)
- [No, MCPs have NOT won (Yet) — Victor Dibia](https://newsletter.victordibia.com/p/no-mcps-have-not-won-yet)
- [MCP's Next Phase: Inside the November 2025 Specification](https://medium.com/@dave-patten/mcps-next-phase-inside-the-november-2025-specification-49f298502b03)
- [Building smarter interactions with MCP elicitation (GitHub Blog)](https://github.blog/ai-and-ml/github-copilot/building-smarter-interactions-with-mcp-elicitation-from-clunky-tool-calls-to-seamless-user-experiences/)
- [MCP is Dead; Long Live MCP!](https://chrlschn.dev/blog/2026/03/mcp-is-dead-long-live-mcp/)

### SDK Implementation Issues
- [Implement SEP-1686: Tasks (Python SDK)](https://github.com/modelcontextprotocol/python-sdk/issues/1546)
- [Implement SEP-1686: Tasks (TypeScript SDK)](https://github.com/modelcontextprotocol/typescript-sdk/issues/1060)
- [Implement SEP-1686: Tasks (Kotlin SDK)](https://github.com/modelcontextprotocol/kotlin-sdk/issues/421)
