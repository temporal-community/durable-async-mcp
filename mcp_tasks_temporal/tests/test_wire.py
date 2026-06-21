# ABOUTME: Round-trip tests for the MCP Tasks extension wire types.
# Verifies camelCase serialization, the DetailedTask variants, and capability detection.

from mcp_tasks_temporal import wire


def test_create_task_result_serializes_camelcase():
    r = wire.CreateTaskResult(
        task_id="t-1",
        status="working",
        created_at="2026-06-20T10:00:00Z",
        last_updated_at="2026-06-20T10:00:00Z",
        ttl_ms=60000,
        poll_interval_ms=500,
    )
    assert r.to_wire() == {
        "resultType": "task",
        "taskId": "t-1",
        "status": "working",
        "createdAt": "2026-06-20T10:00:00Z",
        "lastUpdatedAt": "2026-06-20T10:00:00Z",
        "ttlMs": 60000,
        "pollIntervalMs": 500,
    }


def test_get_task_result_input_required_carries_input_requests():
    r = wire.GetTaskResult(
        task_id="t-1",
        status="input_required",
        created_at="x",
        last_updated_at="x",
        input_requests={
            "approval": wire.InputRequest(
                method="elicitation/create",
                params={
                    "mode": "form",
                    "message": "Approve?",
                    "requestedSchema": {"type": "object"},
                },
            )
        },
    )
    w = r.to_wire()
    assert w["status"] == "input_required"
    assert w["inputRequests"]["approval"]["method"] == "elicitation/create"
    # absent optional fields are dropped
    assert "result" not in w and "error" not in w


def test_get_task_result_completed_carries_result():
    r = wire.GetTaskResult(
        task_id="t-1",
        status="completed",
        created_at="x",
        last_updated_at="x",
        result={"content": [{"type": "text", "text": "PAID"}], "isError": False},
    )
    w = r.to_wire()
    assert w["result"]["content"][0]["text"] == "PAID"
    assert "inputRequests" not in w


def test_update_task_params_parse_by_alias():
    # Mimics the runner: validate by alias only (by_name=False).
    p = wire.UpdateTaskRequestParams.model_validate(
        {
            "taskId": "t-1",
            "inputResponses": {
                "approval": {"action": "accept", "content": {"value": "approve"}}
            },
        },
        by_name=False,
    )
    assert p.task_id == "t-1"
    assert p.input_responses["approval"].action == "accept"
    assert p.input_responses["approval"].content == {"value": "approve"}


def test_get_task_params_parse_by_alias_with_meta():
    p = wire.GetTaskRequestParams.model_validate(
        {"taskId": "t-1", "_meta": wire.client_capability_meta()},
        by_name=False,
    )
    assert p.task_id == "t-1"
    assert wire.declares_tasks_extension(p.meta) is True


def test_declares_tasks_extension_false_when_absent():
    assert wire.declares_tasks_extension(None) is False
    assert wire.declares_tasks_extension({}) is False
    assert (
        wire.declares_tasks_extension(
            {"io.modelcontextprotocol/clientCapabilities": {"extensions": {}}}
        )
        is False
    )
