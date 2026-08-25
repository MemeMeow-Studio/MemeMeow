"""任务、图片阶段和 OpenCode 结果边界的黑盒契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.image_stage_plan import ImageStagePlan, ImageStagePlanError, image_task_requires_single_attempt
from backend.opencode_result_store import OpenCodeResultStore, ResultStoreError
from backend.runtime_execution import AttemptFence, ExecutionAttempt, ExecutionBinding, ExecutionBindingError


def test_execution_binding_fences_attempt_and_scope() -> None:
    """旧 attempt、错误 scope 和错误 generation 不能通过同一绑定。"""
    binding = ExecutionBinding(
        task_id="task-1",
        attempt_id="attempt-1",
        scope_id="scope-1",
        claim_generation=3,
        workspace_selector="scope-1",
    )
    fence = AttemptFence()
    fence.bind(binding)
    assert fence.accepts(binding)
    assert not fence.accepts(ExecutionBinding(task_id="task-1", attempt_id="attempt-2", scope_id="scope-1", claim_generation=3, workspace_selector="scope-1"))
    with pytest.raises(ExecutionBindingError):
        binding.require_matches({"task_id": "task-1", "attempt_id": "attempt-1", "scope_id": "scope-2", "claim_generation": 3, "workspace_selector": "scope-1"})


def test_execution_attempt_unknown_is_terminal_and_not_replayable() -> None:
    """外部副作用状态未知时，attempt 只能收束为 unknown_execution。"""
    binding = ExecutionBinding(task_id="task-1", attempt_id="attempt-1", scope_id="scope-1")
    attempt = ExecutionAttempt(binding).started()
    unknown = attempt.unknown("agent_executor_unavailable")
    assert unknown.status == "unknown_execution"
    assert unknown.terminal
    with pytest.raises(ExecutionBindingError):
        unknown.started()


def test_image_stage_plan_stops_after_failure_and_requires_single_attempt() -> None:
    """固定阶段不越过失败阶段，图片叶子任务使用显式新 Task 重试。"""
    plan = ImageStagePlan(auto_name=False)
    assert plan.next_stage({"visual": "queued"}) == "visual"
    assert plan.next_stage({"visual": "succeeded", "agent": "failed", "text_embedding": "queued"}) is None
    assert plan.next_stage({"visual": "succeeded", "agent": "succeeded", "text_embedding": "queued"}) == "text_embedding"
    assert image_task_requires_single_attempt("meme_context_generation", submission_mode="pipeline")
    assert not image_task_requires_single_attempt("cache_generation", submission_mode="pipeline")
    with pytest.raises(ImageStagePlanError):
        plan.can_run("unknown", {})


def test_result_store_rejects_symlink_and_accepts_atomic_json(tmp_path: Path) -> None:
    """结果 store 拒绝符号链接，并可通过专属目录原子读取 JSON。"""
    store = OpenCodeResultStore(tmp_path / "results", max_bytes=1024)
    draft, result = store.prepare("task-1")
    result.write_text(json.dumps({"ok": True}), encoding="utf-8")
    assert store.read_json(result)["ok"] is True
    draft.unlink(missing_ok=True)
    result.unlink(missing_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    result.symlink_to(outside)
    with pytest.raises(ResultStoreError) as error:
        store.read_json(result)
    assert error.value.code == "agent_result_path_invalid"
