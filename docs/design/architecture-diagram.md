# Architecture Diagram

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MCP CLI Client                                     │
│                           (python -m mcp_client)                                │
│                                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────────────────────────────┐ │
│  │    Human      │    │   OpenAI     │    │      FastMCP Client               │ │
│  │   (stdin)     │    │ Responses API│    │                                   │ │
│  │              │    │              │    │  call_tool()  get_task_status()   │ │
│  │  Elicitation │    │  Tool select │    │  list_tasks() get_task_result()   │ │
│  │  responses   │    │  + reasoning │    │                                   │ │
│  └──────┬───────┘    └──────┬───────┘    └──────────────┬─────────────────────┘ │
│         │                   │                           │                       │
│         │    main.py        │     llm.py                │                       │
│         └───────────────────┴───────────────────────────┘                       │
│                       Chat loop + _poll_and_resolve_task()                       │
└─────────────────────────────────┬───────────────────────────────────────────────┘
                                  │
                            stdio transport
                         (MCP protocol over JSON-RPC)
                                  │
┌─────────────────────────────────┴───────────────────────────────────────────────┐
│                              MCP Server                                         │
│                             (server.py)                                          │
│                                                                                 │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │                          FastMCP (server.py)                               │ │
│  │                                                                            │ │
│  │  Tools:                                                                    │ │
│  │    process_invoice  (task=required)  ─── starts workflow, returns task ID  │ │
│  │    invoice_status   (no task)        ─── queries workflow directly         │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │              Temporal Task Handlers (temporal_task_handlers.py)             │ │
│  │              Overwrites FastMCP's default Docket/Redis handlers            │ │
│  │                                                                            │ │
│  │  tools/call ──> _make_wrapped_call_tool()  ── intercepts process_invoice  │ │
│  │  tasks/get  ──> handle_tasks_get()         ── queries GetInvoiceStatus    │ │
│  │  tasks/result > handle_tasks_result()      ── elicitation + signal + wait │ │
│  │  tasks/list ──> handle_tasks_list()        ── list_workflows              │ │
│  │  tasks/cancel > handle_tasks_cancel()      ── cancel workflow             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────┬───────────────────────────────────────────────┘
                                  │
                         Temporal Python SDK
                        (gRPC to Temporal Server)
                                  │
┌─────────────────────────────────┴───────────────────────────────────────────────┐
│                           Temporal Server                                        │
│                      (temporal server start-dev)                                 │
│                                                                                 │
│  Task Queue: invoice-task-queue                                                 │
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐       │
│  │  Workflow: InvoiceWorkflow  (workflow ID = MCP task ID)              │       │
│  │                                                                      │       │
│  │  Queries:                   Signals:                                 │       │
│  │    GetInvoiceStatus           ApproveInvoice                         │       │
│  │    GetInvoiceData             RejectInvoice                          │       │
│  │    IsInvoiceApproved                                                 │       │
│  │                                                                      │       │
│  │  State Machine:                                                      │       │
│  │  INITIALIZING ─> PENDING-VALIDATION ─> PENDING-APPROVAL ─┐          │       │
│  │                        │                    │              │          │       │
│  │                     (fail)              (approve)      (reject)       │       │
│  │                        │                    │              │          │       │
│  │                        v                    v              v          │       │
│  │                     FAILED              APPROVED ──>   REJECTED      │       │
│  │                                            │                         │       │
│  │                                         PAYING                       │       │
│  │                                         /    \                       │       │
│  │                                        v      v                      │       │
│  │                                     PAID    FAILED                   │       │
│  │                                                                      │       │
│  │  Child Workflows:                                                    │       │
│  │    PayLineItem (one per invoice line, run in parallel)               │       │
│  │      - Waits until due date                                          │       │
│  │      - Calls payment_gateway activity                                │       │
│  └──────────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────────┘
                                  │
                           Worker (worker.py)
                      polls invoice-task-queue
                                  │
┌─────────────────────────────────┴───────────────────────────────────────────────┐
│                        Activities (activities.py)                                │
│                                                                                 │
│  validate_against_erp    ── simulated ERP validation (random/forced failures)   │
│  payment_gateway         ── simulated payment (retries, INSUFFICIENT_FUNDS)     │
└─────────────────────────────────────────────────────────────────────────────────┘


## Key Design Decisions

  Temporal workflow ID = MCP task ID (no lookup table)
  Custom task handlers bypass Docket/Redis entirely
  Elicitation happens inside tasks/result handler at PENDING-APPROVAL
  Client polls tasks/get (not ToolTask.status() which caches stale results)


## State Mapping (Temporal -> MCP)

  INITIALIZING, PENDING-VALIDATION, APPROVED, PAYING  ->  working
  PENDING-APPROVAL                                     ->  input_required
  PAID, REJECTED                                       ->  completed
  FAILED                                               ->  failed
```
