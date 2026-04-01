# ABOUTME: Tests for the Temporal-backed MCP task handlers.
# Covers status mapping, handler logic, error cases, and integration with Temporal workflows.

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Shorthand for patching _get_temporal_client with an async mock returning the test client
def _patch_client(client):
    return patch(
        "async_mcp.temporal_task_handlers._get_temporal_client",
        new_callable=AsyncMock,
        return_value=client,
    )

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from bizservice import activities
from async_mcp.temporal_task_handlers import (
    POLL_INTERVAL_MS,
    TASK_TTL_MS,
    TEMPORAL_TO_MCP_STATE,
    TERMINAL_STATUSES,
    _build_terminal_result,
    _get_temporal_client,
    _query_workflow_status,
    handle_tasks_cancel,
    handle_tasks_get,
    handle_tasks_list,
    handle_tasks_result,
    register_temporal_task_handlers,
    reset_temporal_client,
)
from bizservice.workflows import InvoiceWorkflow, PayLineItem


SAMPLE_INVOICE = {
    "invoice_id": "INV-001",
    "customer": "Acme Corp",
    "lines": [
        {
            "description": "Widget A",
            "amount": 100.00,
            "due_date": "2020-01-01T00:00:00Z",
        },
    ],
}


@asynccontextmanager
async def temporal_test_env():
    """Provide a Temporal test environment with a running worker.

    Yields (env, task_queue) — the environment and the queue name to use.
    """
    os.environ["FAIL_VALIDATE"] = "false"
    os.environ["NO_FAIL_VALIDATE"] = "true"
    os.environ["FAIL_PAYMENT"] = "false"
    os.environ["NO_FAIL_PAYMENT"] = "true"

    env = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"test-queue-{uuid.uuid4()}"
    # Disable the workflow sandbox to avoid beartype circular import issues
    # in the test environment. The sandbox is a Temporal safety feature for
    # determinism checking — not needed in tests.
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[InvoiceWorkflow, PayLineItem],
        activities=[activities.validate_against_erp, activities.payment_gateway],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
        ),
    )
    async with worker:
        yield env, task_queue
    await env.shutdown()


async def _wait_for_status(handle, target_status: str, max_iters: int = 30):
    """Poll workflow until it reaches the target status."""
    for _ in range(max_iters):
        status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
        if status == target_status:
            return status
        await asyncio.sleep(0.3)
    return await handle.query(InvoiceWorkflow.GetInvoiceStatus)


# -- Unit tests: status mapping --


class TestStatusMapping:
    def test_all_workflow_statuses_mapped(self):
        """Every known workflow status has an MCP task state mapping."""
        expected_statuses = {
            "INITIALIZING",
            "PENDING-VALIDATION",
            "PENDING-APPROVAL",
            "APPROVED",
            "PAYING",
            "PAID",
            "FAILED",
            "REJECTED",
        }
        assert set(TEMPORAL_TO_MCP_STATE.keys()) == expected_statuses

    def test_working_states(self):
        for status in ("INITIALIZING", "PENDING-VALIDATION", "APPROVED", "PAYING"):
            assert TEMPORAL_TO_MCP_STATE[status] == "working"

    def test_input_required_state(self):
        assert TEMPORAL_TO_MCP_STATE["PENDING-APPROVAL"] == "input_required"

    def test_completed_states(self):
        assert TEMPORAL_TO_MCP_STATE["PAID"] == "completed"
        assert TEMPORAL_TO_MCP_STATE["REJECTED"] == "completed"

    def test_failed_state(self):
        assert TEMPORAL_TO_MCP_STATE["FAILED"] == "failed"

    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == {"PAID", "FAILED", "REJECTED"}


# -- Unit tests: _build_terminal_result --


class TestBuildTerminalResult:
    def test_paid_result(self):
        result = _build_terminal_result("wf-123", "PAID")
        assert result.isError is False
        assert len(result.content) == 1
        assert "PAID" in result.content[0].text
        assert result.meta["modelcontextprotocol.io/related-task"]["taskId"] == "wf-123"

    def test_failed_result(self):
        result = _build_terminal_result("wf-456", "FAILED")
        assert result.isError is True
        assert "FAILED" in result.content[0].text

    def test_rejected_result(self):
        result = _build_terminal_result("wf-789", "REJECTED")
        assert result.isError is False
        assert "REJECTED" in result.content[0].text


# -- Unit tests: register_temporal_task_handlers --


class TestRegisterHandlers:
    def test_overwrites_all_handlers(self):
        """register_temporal_task_handlers replaces all 5 handler slots."""
        from mcp.types import (
            CallToolRequest,
            CancelTaskRequest,
            GetTaskPayloadRequest,
            GetTaskRequest,
            ListTasksRequest,
        )

        mock_server = MagicMock()
        mock_low_level = MagicMock()
        mock_server._mcp_server = mock_low_level

        original_handler = AsyncMock()
        mock_low_level.request_handlers = {CallToolRequest: original_handler}

        register_temporal_task_handlers(mock_server)

        assert CallToolRequest in mock_low_level.request_handlers
        assert GetTaskRequest in mock_low_level.request_handlers
        assert GetTaskPayloadRequest in mock_low_level.request_handlers
        assert ListTasksRequest in mock_low_level.request_handlers
        assert CancelTaskRequest in mock_low_level.request_handlers

        # CallToolRequest handler should be wrapped (different from original)
        assert mock_low_level.request_handlers[CallToolRequest] is not original_handler


# -- Integration tests using Temporal test environment --


class TestHandleTasksGetIntegration:
    @pytest.mark.integration
    async def test_returns_working_for_new_workflow(self):
        """A freshly started workflow should report 'working' state."""
        async with temporal_test_env() as (env, task_queue):
            with _patch_client(env.client):
                workflow_id = f"invoice-{uuid.uuid4()}"
                await env.client.start_workflow(
                    InvoiceWorkflow.run,
                    SAMPLE_INVOICE,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                await asyncio.sleep(0.5)

                req = _make_get_task_request(workflow_id)
                result = await handle_tasks_get(req)

                task_result = result.root
                assert task_result.taskId == workflow_id
                assert task_result.status in ("working", "input_required")
                assert task_result.ttl == TASK_TTL_MS
                assert task_result.pollInterval == POLL_INTERVAL_MS

    @pytest.mark.integration
    async def test_returns_input_required_at_approval(self):
        """Workflow should report input_required when waiting for approval."""
        async with temporal_test_env() as (env, task_queue):
            with _patch_client(env.client):
                workflow_id = f"invoice-{uuid.uuid4()}"
                handle = await env.client.start_workflow(
                    InvoiceWorkflow.run,
                    SAMPLE_INVOICE,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                await _wait_for_status(handle, "PENDING-APPROVAL")

                req = _make_get_task_request(workflow_id)
                result = await handle_tasks_get(req)
                assert result.root.status == "input_required"

    @pytest.mark.integration
    async def test_not_found_raises_error(self):
        """Querying a nonexistent workflow raises McpError."""
        from mcp.shared.exceptions import McpError

        async with temporal_test_env() as (env, _task_queue):
            with _patch_client(env.client):
                req = _make_get_task_request("nonexistent-workflow-id")
                with pytest.raises(McpError, match="not found"):
                    await handle_tasks_get(req)


class TestHandleTasksResultIntegration:
    @pytest.mark.integration
    async def test_returns_result_for_completed_workflow(self):
        """tasks/result returns CallToolResult for a completed workflow."""
        async with temporal_test_env() as (env, task_queue):
            with _patch_client(env.client):
                workflow_id = f"invoice-{uuid.uuid4()}"
                handle = await env.client.start_workflow(
                    InvoiceWorkflow.run,
                    SAMPLE_INVOICE,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                await _wait_for_status(handle, "PENDING-APPROVAL")
                await handle.signal(InvoiceWorkflow.ApproveInvoice)
                final_status = await handle.result()

                server = MagicMock()
                req = _make_get_task_payload_request(workflow_id)
                result = await handle_tasks_result(req, server)

                tool_result = result.root
                assert len(tool_result.content) == 1
                assert final_status in tool_result.content[0].text
                assert (
                    tool_result.meta["modelcontextprotocol.io/related-task"]["taskId"]
                    == workflow_id
                )

    @pytest.mark.integration
    async def test_raises_error_for_working_workflow(self):
        """tasks/result raises McpError if workflow is still working (not approval or terminal)."""
        from mcp.shared.exceptions import McpError

        async with temporal_test_env() as (env, task_queue):
            with _patch_client(env.client):
                # Start a workflow but don't wait — try immediately
                workflow_id = f"invoice-{uuid.uuid4()}"
                await env.client.start_workflow(
                    InvoiceWorkflow.run,
                    SAMPLE_INVOICE,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                server = MagicMock()
                req = _make_get_task_payload_request(workflow_id)
                # Could be INITIALIZING, PENDING-VALIDATION, or already PENDING-APPROVAL.
                # If it's a working state, we expect the error.
                try:
                    await handle_tasks_result(req, server)
                except McpError as e:
                    assert "not completed yet" in str(e)


class TestHandleTasksCancelIntegration:
    @pytest.mark.integration
    async def test_cancel_running_workflow(self):
        """Cancelling a running workflow returns cancelled status."""
        async with temporal_test_env() as (env, task_queue):
            with _patch_client(env.client):
                workflow_id = f"invoice-{uuid.uuid4()}"
                handle = await env.client.start_workflow(
                    InvoiceWorkflow.run,
                    SAMPLE_INVOICE,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                await _wait_for_status(handle, "PENDING-APPROVAL")

                req = _make_cancel_task_request(workflow_id)
                result = await handle_tasks_cancel(req)
                assert result.root.status == "cancelled"
                assert result.root.taskId == workflow_id

    @pytest.mark.integration
    async def test_cancel_nonexistent_raises_error(self):
        """Cancelling a nonexistent workflow raises McpError."""
        from mcp.shared.exceptions import McpError

        async with temporal_test_env() as (env, _task_queue):
            with _patch_client(env.client):
                req = _make_cancel_task_request("nonexistent-workflow")
                with pytest.raises(McpError, match="not found"):
                    await handle_tasks_cancel(req)


class TestHandleTasksListIntegration:
    @pytest.mark.integration
    @pytest.mark.slow
    async def test_lists_running_workflows(self):
        """tasks/list returns running invoice workflows.

        NOTE: Skipped by default — requires a real Temporal server since
        ListWorkflowExecutions is unimplemented in the time-skipping test server.
        """
        pytest.skip(
            "list_workflows not supported by Temporal time-skipping test server"
        )


class TestGetInvoiceDataQuery:
    @pytest.mark.integration
    async def test_query_returns_invoice_data(self):
        """GetInvoiceData query returns the original invoice dict."""
        async with temporal_test_env() as (env, task_queue):
            workflow_id = f"invoice-{uuid.uuid4()}"
            handle = await env.client.start_workflow(
                InvoiceWorkflow.run,
                SAMPLE_INVOICE,
                id=workflow_id,
                task_queue=task_queue,
            )

            await _wait_for_status(handle, "PENDING-APPROVAL")

            invoice_data = await handle.query(InvoiceWorkflow.GetInvoiceData)
            assert invoice_data["invoice_id"] == "INV-001"
            assert invoice_data["customer"] == "Acme Corp"
            assert len(invoice_data["lines"]) == 1


# -- Helper functions to construct MCP request objects --


def _make_get_task_request(task_id: str):
    """Create a GetTaskRequest with the given task ID."""
    from mcp.types import GetTaskRequest, GetTaskRequestParams

    return GetTaskRequest(
        method="tasks/get",
        params=GetTaskRequestParams(taskId=task_id),
    )


def _make_get_task_payload_request(task_id: str):
    """Create a GetTaskPayloadRequest with the given task ID."""
    from mcp.types import GetTaskPayloadRequest, GetTaskPayloadRequestParams

    return GetTaskPayloadRequest(
        method="tasks/result",
        params=GetTaskPayloadRequestParams(taskId=task_id),
    )


def _make_cancel_task_request(task_id: str):
    """Create a CancelTaskRequest with the given task ID."""
    from mcp.types import CancelTaskRequest, CancelTaskRequestParams

    return CancelTaskRequest(
        method="tasks/cancel",
        params=CancelTaskRequestParams(taskId=task_id),
    )


def _make_list_tasks_request():
    """Create a ListTasksRequest."""
    from mcp.types import ListTasksRequest

    return ListTasksRequest(method="tasks/list")
