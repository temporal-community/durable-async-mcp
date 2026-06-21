# ABOUTME: Tests TaskTrackerWorkflow's durable poll/HITL loop under the time-skipping test env.
# Fake activities simulate the server: working -> input_required -> (after tasks/update) completed.

import asyncio
import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow


class FakeActivities:
    def __init__(self) -> None:
        self.polls = 0
        self.submitted: dict | None = None

    @activity.defn(name=models.START_TASK)
    async def start_task(self, inp: models.StartTaskInput) -> str:
        return "task-1"

    @activity.defn(name=models.POLL_TASK)
    async def poll_task(self, task_id: str) -> models.TaskPollResult:
        self.polls += 1
        if self.submitted is not None:
            return models.TaskPollResult(
                status="completed",
                result={
                    "content": [{"type": "text", "text": "PAID"}],
                    "isError": False,
                },
            )
        if self.polls == 1:
            return models.TaskPollResult(status="working", poll_interval_ms=10)
        return models.TaskPollResult(
            status="input_required",
            poll_interval_ms=10,
            input_requests={
                "approval": {
                    "method": "elicitation/create",
                    "params": {"message": "Approve?"},
                }
            },
        )

    @activity.defn(name=models.SUBMIT_TASK_INPUT)
    async def submit_task_input(self, inp: models.SubmitInput) -> None:
        self.submitted = inp.input_responses

    @activity.defn(name=models.CANCEL_TASK)
    async def cancel_task(self, task_id: str) -> None:
        pass


async def _scenario() -> None:
    fake = FakeActivities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="mcp-tasks-test",
            workflows=[TaskTrackerWorkflow],
            activities=[
                fake.start_task,
                fake.poll_task,
                fake.submit_task_input,
                fake.cancel_task,
            ],
        ):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                models.TaskTrackerInput("process", {"x": 1}),
                id=f"tt-{uuid.uuid4()}",
                task_queue="mcp-tasks-test",
            )

            # Wait until the workflow surfaces the elicitation, then answer it.
            pending = None
            for _ in range(100):
                pending = await handle.query(TaskTrackerWorkflow.get_pending_input)
                if pending:
                    break
                await asyncio.sleep(0.05)
            assert pending is not None and "approval" in pending

            await handle.signal(
                TaskTrackerWorkflow.user_decision,
                {"approval": {"action": "accept", "content": {"value": "approve"}}},
            )

            result = await handle.result()

    assert result["status"] == "completed"
    assert result["result"]["content"][0]["text"] == "PAID"
    assert fake.submitted["approval"]["content"]["value"] == "approve"


def test_task_tracker_workflow_lifecycle():
    asyncio.run(_scenario())
