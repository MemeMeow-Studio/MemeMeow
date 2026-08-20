"""scope-aware OpenCode workspace、selector 和 capability 边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import Settings
from backend.opencode import OpenCodeError, OpenCodeRunner, _workspace_opencode_config
from backend.opencode_workspace import (
    DirectoryWorkspaceProvider,
    TrustedWorkspaceContext,
    WorkspaceCapabilityError,
    WorkspaceCapabilitySigner,
    WorkspaceResolutionError,
    build_edit_permission_rules,
)
from backend.tasks import TaskRecord
from executor import server as executor_server


def _workspace_tree(root: Path, selector: str) -> None:
    """创建测试 provider 要求的固定只读视图。"""
    base = root / selector
    for name in ("workspace", "images", "metadata", "skills"):
        (base / name).mkdir(parents=True)
    (base / "images" / "sample.png").write_bytes(b"image")
    (base / "metadata" / "sample.json").write_text("{}", encoding="utf-8")
    (base / "skills" / "SKILL.md").write_text("skill", encoding="utf-8")


def test_external_selectors_share_db_but_not_workspace_paths(tmp_path: Path) -> None:
    """不同 selector 复用 runtime DB，同时隔离工作目录和输入视图。"""
    runtime = tmp_path / "runtime"
    root = tmp_path / "workspaces"
    _workspace_tree(root, "scope-a")
    _workspace_tree(root, "scope-b")
    provider = DirectoryWorkspaceProvider(runtime, root, selector_for_scope=lambda scope: scope)

    first = provider.resolve(TrustedWorkspaceContext("task-a", "attempt-a", "scope-a", image_relative_path="sample.png"))
    second = provider.resolve(TrustedWorkspaceContext("task-b", "attempt-b", "scope-b", image_relative_path="sample.png"))

    assert first.db_path == second.db_path == runtime / "opencode.db"
    assert first.directory != second.directory
    assert first.config_file != second.config_file
    assert first.config_dir != second.config_dir
    assert first.image_path("sample.png") != second.image_path("sample.png")
    assert dict(first.permission_rules)["*"] == "deny"
    assert dict(first.permission_rules)[f"{first.task_results_root.absolute()}/*"] == "allow"
    edit_rules = dict(
        build_edit_permission_rules(
            task_scratch_root=first.task_scratch_root,
            config_file=first.config_file,
            config_dir=first.config_dir,
            draft_path=first.draft_path,
            result_path=first.result_path,
        )
    )
    assert edit_rules["*"] == "deny"
    assert edit_rules[f"**/{first.config_file.as_posix().lstrip('/')}"] == "deny"
    assert edit_rules[f"**/{first.draft_path.as_posix().lstrip('/')}"] == "allow"
    assert not first.task_scratch_root.exists()
    assert not first.task_results_root.exists()
    repeat = provider.resolve(TrustedWorkspaceContext("task-a", "attempt-retry", "scope-a", selector="scope-a", image_relative_path="sample.png"))
    assert repeat.directory == first.directory
    assert repeat.config_file == first.config_file
    assert repeat.db_path == first.db_path


def test_workspace_config_separates_external_access_from_edit_access(tmp_path: Path) -> None:
    """外部目录父级检查与普通编辑权限必须共同收窄到结果协议。"""
    runtime = tmp_path / "runtime"
    root = tmp_path / "workspaces"
    _workspace_tree(root, "scope-a")
    provider = DirectoryWorkspaceProvider(runtime, root, selector_for_scope=lambda scope: scope)
    workspace = provider.resolve(TrustedWorkspaceContext("task", "attempt", "scope-a", image_relative_path="sample.png"))

    config = _workspace_opencode_config(workspace)
    external = config["permission"]["external_directory"]
    edit = config["permission"]["edit"]
    assert external[f"{workspace.task_results_root.absolute()}/*"] == "allow"
    assert edit["*"] == "deny"
    assert edit[f"**/{workspace.draft_path.as_posix().lstrip('/')}"] == "allow"
    assert edit[f"**/{workspace.config_file.as_posix().lstrip('/')}"] == "deny"


def test_selector_mapping_and_symlink_fail_closed(tmp_path: Path) -> None:
    """非法映射、未知目录和 workspace 符号链接不会创建可执行路径。"""
    runtime = tmp_path / "runtime"
    root = tmp_path / "workspaces"
    _workspace_tree(root, "known")
    provider = DirectoryWorkspaceProvider(runtime, root, selector_for_scope=lambda _scope: "../outside")
    with pytest.raises(WorkspaceResolutionError) as error:
        provider.resolve(TrustedWorkspaceContext("task", "attempt", "scope"))
    assert error.value.code == "opencode_workspace_invalid"

    link_root = tmp_path / "linked"
    link_root.symlink_to(root, target_is_directory=True)
    linked_provider = DirectoryWorkspaceProvider(runtime, link_root, selector_for_scope=lambda _scope: "known")
    with pytest.raises(WorkspaceResolutionError):
        linked_provider.resolve(TrustedWorkspaceContext("task", "attempt", "scope"))

    reserved_provider = DirectoryWorkspaceProvider(root.parent, root, selector_for_scope=lambda _scope: "local")
    with pytest.raises(WorkspaceResolutionError) as error:
        reserved_provider.resolve(TrustedWorkspaceContext("task", "attempt", "scope"))
    assert error.value.code == "opencode_workspace_invalid"

    incomplete_root = root / "incomplete"
    incomplete_root.mkdir()
    (incomplete_root / "images").mkdir()
    (incomplete_root / "metadata").mkdir()
    (incomplete_root / "skills").mkdir()
    incomplete_provider = DirectoryWorkspaceProvider(runtime, root, selector_for_scope=lambda _scope: "incomplete")
    with pytest.raises(WorkspaceResolutionError) as error:
        incomplete_provider.resolve(TrustedWorkspaceContext("task", "attempt", "scope"))
    assert error.value.code == "opencode_workspace_invalid"
    assert not (incomplete_root / "workspace").exists()


def test_workspace_capability_binds_attempt_selector_and_resume(tmp_path: Path) -> None:
    """capability 的签名、期限、selector 和 resume 绑定必须全部成立。"""
    signer = WorkspaceCapabilitySigner("workspace-secret", clock=lambda: 100)
    token = signer.issue(task_id="task", attempt_id="attempt", selector="scope-a", session_id="session", resume_of_attempt_id="old")
    claims = signer.verify(token, task_id="task", attempt_id="attempt", selector="scope-a", session_id="session", resume_of_attempt_id="old")
    assert claims["workspace_selector"] == "scope-a"

    with pytest.raises(WorkspaceCapabilityError) as error:
        signer.verify(token, task_id="task", attempt_id="attempt", selector="scope-b", session_id="session", resume_of_attempt_id="old")
    assert error.value.code == "opencode_workspace_mismatch"

    expired = WorkspaceCapabilitySigner("workspace-secret", clock=lambda: 500)
    with pytest.raises(WorkspaceCapabilityError) as error:
        expired.verify(token, task_id="task", attempt_id="attempt", selector="scope-a", session_id="session", resume_of_attempt_id="old")
    assert error.value.code == "opencode_workspace_capability_expired"

    with pytest.raises(WorkspaceCapabilityError) as error:
        signer.verify(token, task_id="task", attempt_id="attempt", selector="scope-a", session_id="session", resume_of_attempt_id="old", now=400)
    assert error.value.code == "opencode_workspace_capability_expired"


def test_workspace_capability_low_level_sign_cannot_extend_ttl() -> None:
    """低层 sign 入口也必须遵守 signer 的最大有效期。"""
    signer = WorkspaceCapabilitySigner("workspace-secret", ttl_seconds=60, clock=lambda: 100)
    claims = {
        "v": 1,
        "task_id": "task",
        "attempt_id": "attempt",
        "workspace_selector": "scope-a",
        "audience": "mememeow-agent-executor",
        "exp": 100 + 61,
    }
    with pytest.raises(WorkspaceCapabilityError) as error:
        signer.sign(claims)
    assert error.value.code == "opencode_workspace_capability_invalid"


@pytest.mark.parametrize("relative", ["nested/./sample.png", "nested//sample.png", "C:/sample.png", "C:sample.png", "sample.png\x00"])
def test_relative_image_paths_reject_normalized_or_control_segments(relative: str) -> None:
    """原始路径段在归一化前也必须拒绝，避免绕过相对路径契约。"""
    with pytest.raises(WorkspaceResolutionError, match="相对路径无效"):
        TrustedWorkspaceContext("task", "attempt", "scope", image_relative_path=relative)
    with pytest.raises(ValueError, match="agent_image_path_forbidden"):
        executor_server._relative_image_path(relative)


def test_corrupt_persisted_workspace_selector_disables_resume() -> None:
    """损坏的持久 selector 不能清洗成 None 后继续暴露可恢复 session。"""
    record = TaskRecord.from_dict(
        {
            "task_id": "task",
            "task_type": "meme_context_generation",
            "status": "failed",
            "resume_available": True,
            "resume_reason": "session_resumable",
            "session_id": "session-task",
            "executor_attempt_id": "attempt-task",
            "workspace_selector": "../outside",
        }
    )
    snapshot = record.as_dict()
    assert snapshot["resume_available"] is False
    assert snapshot["resume_reason"] == "opencode_workspace_mismatch"


def test_executor_resolves_signed_selector_to_current_image_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """executor 只接受签名 selector，并将图片解析到对应 workspace 视图。"""
    runtime = tmp_path / "runtime"
    image_root = tmp_path / "images"
    skill_root = tmp_path / "skill"
    selector_root = tmp_path / "selector-root"
    runtime.mkdir()
    image_root.mkdir()
    skill_root.mkdir()
    (image_root / "sample.png").write_bytes(b"legacy")
    _workspace_tree(selector_root, "scope-a")
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(executor_server, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(executor_server, "WORKSPACE", runtime / "workspace")
    monkeypatch.setattr(executor_server, "RESULT_ROOT", runtime / "task-results")
    monkeypatch.setattr(executor_server, "LOG_ROOT", runtime / "logs")
    monkeypatch.setattr(executor_server, "IMAGE_ROOT", image_root)
    monkeypatch.setattr(executor_server, "SKILL_ROOT", skill_root)
    monkeypatch.setattr(executor_server, "WORKSPACE_ROOT", selector_root)
    monkeypatch.setenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "executor-token")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_EXECUTABLE", str(fake))
    monkeypatch.setenv("MEMEMEOW_WORKSPACE_CAPABILITY_KEY", "capability-key")
    executor = executor_server.Executor()
    try:
        signer = WorkspaceCapabilitySigner("capability-key")
        token = signer.issue(task_id="task", attempt_id="attempt", selector="scope-a")
        values = executor._validate_request(
            {
                "task_id": "task",
                "executor_attempt_id": "attempt",
                "image_relative_path": "sample.png",
                "workspace_selector": "scope-a",
                "workspace_capability": token,
            }
        )
        layout = values["workspace_layout"]
        assert isinstance(layout, executor_server.WorkspaceLayout)
        assert layout.images_root == selector_root / "scope-a" / "images"
        assert layout.directory == selector_root / "scope-a" / "workspace"
        assert layout.config_file == layout.directory / "tasks" / "task" / "opencode.json"
        assert layout.config_dir == layout.directory / "tasks" / "task" / ".opencode"
        assert not (runtime / "task-results" / "task").exists()
        assert not (layout.directory / "tasks" / "task").exists()
        clock = [100]
        short_signer = WorkspaceCapabilitySigner("capability-key", ttl_seconds=1, clock=lambda: clock[0])
        short_token = short_signer.issue(task_id="queued", attempt_id="queued-attempt", selector="scope-a")
        executor.capability_signer = short_signer
        queued_task = executor_server.TaskState(
            task_id="queued",
            business_task_id="queued",
            executor_attempt_id="queued-attempt",
            image_relative_path="sample.png",
            reverse_image_policy="forbid",
            timeout_seconds=5,
            workspace_selector="scope-a",
            workspace_capability=short_token,
        )
        clock[0] = 101
        with pytest.raises(RuntimeError, match="opencode_workspace_capability_expired"):
            executor._verify_task_workspace_capability(queued_task)
        with pytest.raises(ValueError, match="opencode_workspace_mismatch"):
            executor._validate_request(
                {
                    "task_id": "task-2",
                    "executor_attempt_id": "attempt-2",
                    "image_relative_path": "sample.png",
                    "workspace_selector": "scope-a",
                "workspace_capability": token,
                }
            )
        assert not (runtime / "task-results" / "task-2").exists()
        assert not (layout.directory / "tasks" / "task-2").exists()
    finally:
        executor.close()


def test_runner_rejects_external_workspace_without_capability_before_task_side_effects(tmp_path: Path) -> None:
    """executor 外部 workspace 缺少 capability 时不能创建 Task 临时或结果目录。"""
    runtime = tmp_path / "runtime"
    root = tmp_path / "workspaces"
    _workspace_tree(root, "scope-a")
    settings = Settings(
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        opencode_runtime_root=runtime,
        opencode_model="gpt-5.6-luna",
        opencode_base_url="https://example.invalid/v1",
        opencode_api_key="api-key",
        agent_runtime_mode="executor",
        agent_executor_url="http://agent:8277",
        agent_executor_token="executor-token",
    )
    runner = OpenCodeRunner(
        settings,
        workspace_provider=DirectoryWorkspaceProvider(runtime, root, selector_for_scope=lambda scope: scope),
    )
    context = TrustedWorkspaceContext("task", "attempt", "scope-a", image_relative_path="sample.png")
    with pytest.raises(OpenCodeError) as error:
        runner.run(tmp_path / "unused.png", lambda *_args: None, task_id="task", workspace_context=context)
    assert error.value.code == "opencode_workspace_capability_unavailable"
    assert not (runtime / "task-results" / "task").exists()
    assert not (root / "scope-a" / "workspace" / "tasks" / "task").exists()
    assert not (runtime / "workspace").exists()
