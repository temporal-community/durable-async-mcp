# ABOUTME: Tests TaskTrackerWorkflow's durable poll/elicitation loop under the time-skipping env.
# Fake activities simulate the server-push model: poll -> input_required -> handle_elicitation -> completed.

import asyncio
import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

# beartype installs import hooks that hit a circular import inside the workflow sandbox; pass through.
_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
)
_RESULT = {"content": [{"type": "text", "text": "PAID"}], "isError": False}


class FakeActivities:
    """Scripts a poll sequence; handle_elicitation returns immediately (the real one waits)."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self._i = 0
        self.elicit_calls = 0

    @activity.defn(name=models.START_TASK)
    async def start_task(self, inp: models.StartTaskInput) -> str:
        return "task-1"

    @activity.defn(name=models.POLL_TASK)
    async def poll_task(self, task_id: str) -> str:
        status = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return status

    @activity.defn(name=models.HANDLE_ELICITATION)
    async def handle_elicitation(self, task_id: str) -> str:
        self.elicit_calls += 1
        return "elicitation_handled"

    @activity.defn(name=models.GET_TASK_RESULT)
    async def get_task_result(self, task_id: str) -> dict:
        return _RESULT


async def _run(fake: FakeActivities, queue: str, env) -> dict:
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskTrackerWorkflow],
        activities=[
            fake.start_task,
            fake.poll_task,
            fake.handle_elicitation,
            fake.get_task_result,
        ],
        workflow_runner=_RUNNER,
    ):
        handle = await env.client.start_workflow(
            TaskTrackerWorkflow.run,
            models.TaskTrackerInput("process", {"x": 1}),
            id=f"tt-{uuid.uuid4()}",
            task_queue=queue,
        )
        return await handle.result()


async def _lifecycle() -> None:
    fake = FakeActivities(["input_required", "completed"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(fake, "mcp-tasks-test", env)
    assert result["status"] == "completed"
    assert result["result"]["content"][0]["text"] == "PAID"
    assert fake.elicit_calls == 1


def test_lifecycle_single_gate():
    asyncio.run(_lifecycle())


async def _two_round() -> None:
    # Two input_required rounds (approval, then cost-center) before completion.
    fake = FakeActivities(["input_required", "input_required", "completed"])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(fake, "mcp-tasks-test", env)
    assert result["status"] == "completed"
    assert fake.elicit_calls == 2


def test_two_round_elicitation():
    asyncio.run(_two_round())


class _HoldingActivities(FakeActivities):
    """handle_elicitation blocks until released, so the workflow stays input_required."""

    def __init__(self) -> None:
        super().__init__(["input_required", "completed"])
        self.release = asyncio.Event()

    @activity.defn(name=models.HANDLE_ELICITATION)
    async def handle_elicitation(self, task_id: str) -> str:
        self.elicit_calls += 1
        await self.release.wait()
        return "elicitation_handled"


async def _signal_surface() -> None:
    fake = _HoldingActivities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="mcp-tasks-surface",
            workflows=[TaskTrackerWorkflow],
            activities=[
                fake.start_task,
                fake.poll_task,
                fake.handle_elicitation,
                fake.get_task_result,
            ],
            workflow_runner=_RUNNER,
        ):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                models.TaskTrackerInput("process", {"x": 1}),
                id=f"tt-{uuid.uuid4()}",
                task_queue="mcp-tasks-surface",
            )
            # Wait until the workflow is parked in handle_elicitation.
            for _ in range(100):
                if (
                    await handle.query(TaskTrackerWorkflow.get_status)
                    == "input_required"
                ):
                    break
                await asyncio.sleep(0.05)
            assert (
                await handle.query(TaskTrackerWorkflow.get_status) == "input_required"
            )

            # The elicitation handler would set _pending_input via this signal; emulate it.
            pending = {
                "approval": {
                    "method": "elicitation/create",
                    "params": {"message": "Approve?", "requestedSchema": {}},
                }
            }
            await handle.signal(TaskTrackerWorkflow.elicitation_received, pending)
            assert await handle.query(TaskTrackerWorkflow.get_pending_input) == pending

            decision = {
                "approval": {"action": "accept", "content": {"value": "approve"}}
            }
            await handle.signal(TaskTrackerWorkflow.user_decision, decision)
            assert (
                await handle.query(TaskTrackerWorkflow.get_pending_decision) == decision
            )
            assert await handle.query(TaskTrackerWorkflow.get_task_id) == "task-1"

            fake.release.set()
            result = await handle.result()
    assert result["status"] == "completed"


def test_signal_and_query_surface():
    asyncio.run(_signal_surface())
