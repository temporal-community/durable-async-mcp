# ABOUTME: Integration seam letting a bare CreateTaskResult pass tools/call validation in mcp 2.0.0a2.
# The SDK validates tools/call results against a closed union (no task variant); this is the one
# core gap "native tasks" must close upstream (widen the result surface, or add a result-type
# registration hook) — at which point this monkeypatch becomes a clean SDK call. See README "Path to native".

from __future__ import annotations

from importlib.metadata import version as _pkg_version

import mcp.types.methods as _methods

# SDK versions this shim has been verified against. A bump should fail the guard
# test loudly (one place) so we re-verify rather than silently mis-patch.
TARGETED_SDK_VERSIONS: frozenset[str] = frozenset({"2.0.0a1", "2.0.0a2"})

_PATCH_MARKER = "_tasks_passthrough_shim"


def installed_sdk_version() -> str:
    return _pkg_version("mcp")


def _is_task_result(method: str, data: object) -> bool:
    # Narrow predicate: only a tools/call result carrying the task discriminator.
    return (
        method == "tools/call"
        and isinstance(data, dict)
        and data.get("resultType") == "task"
    )


def _patch(name: str, task_return: str) -> None:
    orig = getattr(_methods, name)
    if getattr(orig, _PATCH_MARKER, False):
        return  # idempotent: already installed

    def shim(method: str, version: str, data, **kwargs):  # type: ignore[no-untyped-def]
        if _is_task_result(method, data):
            return data if task_return == "data" else None
        return orig(method, version, data, **kwargs)

    setattr(shim, _PATCH_MARKER, True)
    shim._original = orig  # type: ignore[attr-defined]
    setattr(_methods, name, shim)


def install_tasks_result_passthrough() -> None:
    """Allow a bare ``CreateTaskResult`` (resultType=="task") through tools/call validation.

    Patches the two seams the SDK validates results at — ``serialize_server_result``
    (server outbound) and ``validate_server_result`` (client inbound). Anything that
    isn't a tools/call task result delegates to the original, unchanged. Idempotent.
    Install server-side in ``register_tasks_extension`` and client-side when building
    the session.

    This is the integration seam, not throwaway code: it stands in for the one core-SDK
    change "native tasks" requires (a task variant in the tools/call result surface, or a
    result-type registration hook). When that lands upstream, replace these two patches
    with the sanctioned call — the wire types, handlers, and client stay unchanged.
    """
    _patch("serialize_server_result", task_return="data")
    _patch("validate_server_result", task_return="none")


def uninstall_tasks_result_passthrough() -> None:
    """Restore the original SDK functions (used by tests)."""
    for name in ("serialize_server_result", "validate_server_result"):
        fn = getattr(_methods, name)
        if getattr(fn, _PATCH_MARKER, False):
            setattr(_methods, name, fn._original)
