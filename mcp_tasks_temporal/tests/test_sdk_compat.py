# ABOUTME: Guard test for the alpha SDK compat shim (_sdk_compat).
# Fails loudly if the targeted SDK version drifts or the patched seams change shape.

import inspect

import mcp.types.methods as methods
import pytest

from mcp_tasks_temporal import _sdk_compat as compat

A_VERSION = "2026-07-28"


def test_targeted_sdk_version_pinned():
    # A bump must force a human to re-verify the shim, not silently mis-patch.
    assert compat.installed_sdk_version() in compat.TARGETED_SDK_VERSIONS, (
        f"mcp=={compat.installed_sdk_version()} is outside the verified set "
        f"{compat.TARGETED_SDK_VERSIONS}; re-verify _sdk_compat before bumping."
    )


def test_patched_seams_have_expected_signature():
    for name in ("serialize_server_result", "validate_server_result"):
        fn = getattr(methods, name)
        params = list(inspect.signature(fn).parameters)
        assert params[:3] == ["method", "version", "data"], (name, params)


@pytest.fixture
def shim():
    compat.install_tasks_result_passthrough()
    yield
    compat.uninstall_tasks_result_passthrough()


def test_task_result_passes_through_serialize(shim):
    envelope = {"resultType": "task", "taskId": "t-1", "status": "working"}
    assert (
        methods.serialize_server_result("tools/call", A_VERSION, envelope) is envelope
    )


def test_task_result_passes_through_validate(shim):
    envelope = {"resultType": "task", "taskId": "t-1", "status": "working"}
    assert methods.validate_server_result("tools/call", A_VERSION, envelope) is None


def test_normal_result_still_validated(shim):
    # A genuine CallToolResult still goes through real validation (not bypassed).
    out = methods.serialize_server_result(
        "tools/call", A_VERSION, {"content": [], "resultType": "complete"}
    )
    assert out["content"] == []
    # ...and an invalid normal result still raises.
    with pytest.raises(Exception):
        methods.serialize_server_result(
            "tools/call", A_VERSION, {"not": "a valid result"}
        )


def test_install_is_idempotent():
    compat.install_tasks_result_passthrough()
    first = methods.serialize_server_result
    compat.install_tasks_result_passthrough()
    assert methods.serialize_server_result is first
    compat.uninstall_tasks_result_passthrough()


def test_uninstall_restores_original():
    original = methods.serialize_server_result
    compat.install_tasks_result_passthrough()
    assert methods.serialize_server_result is not original
    compat.uninstall_tasks_result_passthrough()
    assert methods.serialize_server_result is original
