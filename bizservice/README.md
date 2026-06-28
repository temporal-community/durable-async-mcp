# Business Service (Temporal Workflows)

Temporal workflows, activities, and worker for invoice processing. This is the core business logic, independent of any MCP server implementation.

## Components

- **`workflows.py`** -- `InvoiceWorkflow` (main orchestrator) and `PayLineItem` (child workflow for each line item)
- **`activities.py`** -- `validate_against_erp`, `payment_gateway` (configurable failure rates), and `reconcile_with_erp` (a slow ERP-post step)
- **`worker.py`** -- Temporal worker that polls `invoice-task-queue`
- **`workflow_cli.py`** -- CLI tool for interacting with workflows directly via Temporal

## Running the Worker

```bash
# Start the Temporal server first
temporal server start-dev

# Start the worker
python -m bizservice.worker [--fail-validate] [--fail-payment]
```

## Workflow CLI

You can interact with workflows directly via Temporal (no MCP server needed) using the CLI tool. This is useful for testing workflows independently or scripting approval flows.

**Prerequisites:** Temporal server and worker must be running.

### Start a workflow

```bash
# Uses samples/invoice_acme.json by default
python -m bizservice.workflow_cli start

# With a specific invoice and workflow ID
python -m bizservice.workflow_cli start --id my-invoice-01 --invoice '{"invoice_id": "INV-200", "customer": "Globex", "lines": [{"description": "Consulting", "amount": 500, "due_date": "2024-07-01T00:00:00Z"}]}'
```

### Query workflow state

```bash
python -m bizservice.workflow_cli status <workflow-id>
python -m bizservice.workflow_cli data <workflow-id>
```

### Approve or reject

```bash
python -m bizservice.workflow_cli approve <workflow-id>
python -m bizservice.workflow_cli reject <workflow-id>
```

### Force immediate payment on a line item

If a `PayLineItem` child workflow is waiting for its due date, you can skip the timer:

```bash
python -m bizservice.workflow_cli force-pay <child-workflow-id>
```

The child workflow ID is visible in the Temporal Web UI (`http://localhost:8233`).

### Example session

```bash
$ python -m bizservice.workflow_cli start --id demo-inv-1
Started workflow: demo-inv-1

$ python -m bizservice.workflow_cli status demo-inv-1
Status: PENDING-APPROVAL

$ python -m bizservice.workflow_cli approve demo-inv-1
Sent approve signal to demo-inv-1

$ python -m bizservice.workflow_cli status demo-inv-1
Status: PAID
```

Use `--address` to connect to a non-default Temporal server (default: `localhost:7233`).

## Workflow Lifecycle

```
INITIALIZING -> PENDING-VALIDATION -> PENDING-APPROVAL -> APPROVED -> RECONCILING ----------------+
                                                       -> REJECTED                                |
                                                                                                  v
                              (total > $5000)  PENDING-COST-CENTER -> CODED ---> PAYING -> PAID / FAILED
                              (total <= $5000)  ----------------------------> PAYING -> PAID / FAILED
```

### InvoiceWorkflow

Validates invoice, waits for approval (up to 5 days), posts to the ERP via the slow `reconcile_with_erp`
step, then for invoices over `COST_CENTER_THRESHOLD` ($5000) takes a **second human gate**
(cost-center coding) before processing line items in parallel via child workflows.

- **Signals:** `ApproveInvoice`, `RejectInvoice`, `SubmitCostCenter(content)`
- **Queries:** `GetInvoiceStatus`, `GetInvoiceData`, `GetCostCenter`

> The `workflow_cli` below covers approve/reject only — it has no `SubmitCostCenter` command. To exercise
> the cost-center gate, drive a large invoice through the MCP client UI, or use a small invoice
> (≤ $5000, e.g. `samples/invoice1.json`) with the CLI to skip it.

### PayLineItem

Child workflow that waits until due date, then calls payment gateway with retry policy (3 attempts, non-retryable for INSUFFICIENT_FUNDS).

- **Signals:** `ForcePayment` (skips the due-date timer and pays immediately)

### Activities

- `validate_against_erp` -- 30% random failure, or forced via `FAIL_VALIDATE=true`, disabled via `NO_FAIL_VALIDATE=true`
- `payment_gateway` -- 5% INSUFFICIENT_FUNDS (non-retryable), 30% retryable failure, or forced via `FAIL_PAYMENT=true`, disabled via `NO_FAIL_PAYMENT=true`
- `reconcile_with_erp` -- posts the approved invoice to the ERP; intentionally slow (delay via `RECONCILE_DELAY_SECONDS`, default 15s) so the MCP client polls several times before the next gate
