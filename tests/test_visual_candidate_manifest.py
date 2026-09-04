"""视觉候选 manifest、provider 物化和候选目录清理测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.config import Settings
from backend.opencode import OpenCodeError, OpenCodeRunner
from backend.opencode_workspace import DirectoryWorkspaceProvider, TrustedWorkspaceContext
from backend.visual_snapshot import build_visual_match_snapshot, visual_match_snapshot_manifest
from backend.visual_candidates import VisualCandidateMaterializationError, _copy_identity, materialize_local_candidates


def _workspace_tree(root: Path, selector: str) -> None:
    """创建外部 provider 要求的最小 scope 目录视图。"""
    base = root / selector
    for name in ("workspace", "images", "metadata", "skills"):
        (base / name).mkdir(parents=True)


def _snapshot() -> dict[str, object]:
    """构造 provider 测试使用的 protocol v2 snapshot。"""
    return build_visual_match_snapshot(
        query_meme_id="query",
        image_sha256="a" * 64,
        model="dinov2_vitb14",
        dimensions=768,
        preprocess_version="dinov2-v1-gif-first-frame",
        candidates=[
            {
                "meme_id": "candidate",
                "image_sha256": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
                "size_bytes": 3,
                "score": 0.8,
                "relative_path": "candidate-01.png",
                "context": {"title": "参考"},
            }
        ],
        matched_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _settings(tmp_path: Path) -> Settings:
    """构造只用于 workspace 解析的隔离设置。"""
    return Settings(
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        opencode_runtime_root=tmp_path / "runtime",
        opencode_model="gpt-5.6-luna",
        opencode_base_url="https://example.invalid/v1",
        opencode_api_key="api-key",
        agent_runtime_mode="host",
    )


def test_runner_materializes_candidates_before_returning_workspace(tmp_path: Path) -> None:
    """provider hook 必须写入固定 manifest，Runner 返回前会拒绝缺失文件。"""
    workspace_root = tmp_path / "workspaces"
    _workspace_tree(workspace_root, "scope-a")
    snapshot = _snapshot()
    calls: list[str] = []

    class Provider(DirectoryWorkspaceProvider):
        """把测试 snapshot 写入 task-scoped candidate 目录的 provider。"""

        def prepare_candidates(self, context, resolved) -> None:
            """写入脱敏 manifest，验证 hook 收到同一任务 snapshot。"""
            assert context.visual_match_snapshot == snapshot
            calls.append(context.task_id)
            resolved.candidate_root.mkdir(parents=True, exist_ok=True)
            candidate_path = resolved.candidate_root / "candidate-01.png"
            candidate_path.write_bytes(b"foo")
            candidate_path.chmod(0o444)
            resolved.candidate_manifest_path.write_text(
                json.dumps(visual_match_snapshot_manifest(context.visual_match_snapshot), ensure_ascii=False),
                encoding="utf-8",
            )
            resolved.candidate_manifest_path.chmod(0o444)
            resolved.candidate_root.chmod(0o555)

    provider = Provider(tmp_path / "runtime", workspace_root, selector_for_scope=lambda _scope: "scope-a")
    runner = OpenCodeRunner(_settings(tmp_path), workspace_provider=provider)
    try:
        resolved = runner.resolve_workspace(
            TrustedWorkspaceContext("task", "attempt", "scope-a", visual_match_snapshot=snapshot)
        )
        assert calls == ["task"]
        assert resolved.candidate_manifest_path.is_file()
        manifest = json.loads(resolved.candidate_manifest_path.read_text(encoding="utf-8"))
        assert manifest["snapshot_sha256"] == snapshot["snapshot_sha256"]
        assert "storage_key" not in json.dumps(manifest, ensure_ascii=False)
    finally:
        runner.shutdown()


def test_runner_rejects_materializer_symlink(tmp_path: Path) -> None:
    """候选物化不能把 task 根替换为符号链接。"""
    workspace_root = tmp_path / "workspaces"
    _workspace_tree(workspace_root, "scope-a")
    outside = tmp_path / "outside"
    outside.mkdir()

    class Provider(DirectoryWorkspaceProvider):
        """写入恶意候选符号链接的测试 provider。"""

        def prepare_candidates(self, _context, resolved) -> None:
            """模拟宿主物化竞态。"""
            resolved.candidate_root.parent.mkdir(parents=True, exist_ok=True)
            resolved.candidate_root.symlink_to(outside, target_is_directory=True)

    provider = Provider(tmp_path / "runtime", workspace_root, selector_for_scope=lambda _scope: "scope-a")
    runner = OpenCodeRunner(_settings(tmp_path), workspace_provider=provider)
    try:
        with pytest.raises(OpenCodeError) as error:
            runner.resolve_workspace(
                TrustedWorkspaceContext("task", "attempt", "scope-a", visual_match_snapshot=_snapshot())
            )
        assert error.value.code == "opencode_workspace_invalid"
    finally:
        runner.shutdown()


def test_local_materializer_rechecks_candidate_source_on_resume(tmp_path: Path) -> None:
    """resume 复用只读目录前必须再次确认候选 Meme 和 BlobStore 身份。"""
    snapshot = _snapshot()
    candidate_root = tmp_path / "candidates" / "task"
    candidate_root.mkdir(parents=True)
    (candidate_root / "candidate-01.png").write_bytes(b"foo")
    (candidate_root / "manifest.json").write_text(
        json.dumps(visual_match_snapshot_manifest(snapshot), ensure_ascii=False),
        encoding="utf-8",
    )
    (candidate_root / "candidate-01.png").chmod(0o444)
    (candidate_root / "manifest.json").chmod(0o444)
    candidate_root.chmod(0o555)

    class Environment:
        """返回当前 scope 候选 Meme 的最小资源夹具。"""

        def __init__(self, meme) -> None:
            """保存可变候选 Meme。"""
            self.memes = type("Memes", (), {"get": lambda _self, _meme_id: meme})()

        def __enter__(self):
            """进入 scope 资源上下文。"""
            return self

        def __exit__(self, *_args) -> bool:
            """退出 scope 资源上下文。"""
            return False

    class Blob:
        """模拟按身份校验 BlobStore。"""

        def exists_with_identity(self, _key, *, sha256, size_bytes) -> bool:
            """只接受 snapshot 中的原始内容指纹。"""
            return sha256 == "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae" and size_bytes == 3

    meme = type(
        "Meme",
        (),
        {
            "sha256": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
            "size_bytes": 3,
            "storage_key": "candidate.png",
        },
    )()
    resources = type(
        "Resources",
        (),
        {
            "blob_store_for_scope": lambda _self, _scope: Blob(),
            "environment": lambda _self, _scope: Environment(meme),
        },
    )()
    context = TrustedWorkspaceContext("task", "attempt", "local", visual_match_snapshot=snapshot)
    resolved = type("Resolved", (), {"candidate_root": candidate_root})()

    meme.sha256 = "f" * 64
    with pytest.raises(VisualCandidateMaterializationError):
        materialize_local_candidates(resources, context, resolved)


def test_local_visual_match_script_reads_manifest_without_callback(tmp_path: Path) -> None:
    """Skill 脚本只依赖固定 manifest，不需要视觉 URL 或 callback token。"""
    candidates = tmp_path / "candidates" / "task"
    candidates.mkdir(parents=True)
    manifest = visual_match_snapshot_manifest(_snapshot())
    (candidates / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "MEMEMEOW_AGENT_TASK_ID": "task",
            "MEMEMEOW_AGENT_CANDIDATE_MANIFEST": str(candidates / "manifest.json"),
        }
    )
    environment.pop("MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL", None)
    environment.pop("MEMEMEOW_AGENT_CALLBACK_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "skills/research-meme-context/scripts/local_visual_match.py"],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["snapshot_sha256"] == manifest["snapshot_sha256"]


def test_cleanup_removes_candidate_directory_with_result(tmp_path: Path) -> None:
    """候选目录与结果目录使用同一 retention 保护和清理边界。"""
    runner = OpenCodeRunner(_settings(tmp_path))
    try:
        result_dir = runner.runtime_root / "task-results" / "old-task"
        candidate_dir = runner.runtime_root / "candidates" / "old-task"
        result_dir.mkdir(parents=True)
        candidate_dir.mkdir(parents=True)
        old_mtime = os.path.getmtime(result_dir) - 30 * 86400
        os.utime(result_dir, (old_mtime, old_mtime))
        os.utime(candidate_dir, (old_mtime, old_mtime))
        assert runner.cleanup_task_results() == 1
        assert not result_dir.exists()
        assert not candidate_dir.exists()
    finally:
        runner.shutdown()


def test_local_materializer_rejects_parent_symlink_for_nested_candidate(tmp_path: Path) -> None:
    """嵌套相对路径的父目录不能通过符号链接逃出候选临时根。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"foo")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "candidate-root"
    root.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VisualCandidateMaterializationError):
        _copy_identity(
            source,
            root / "nested" / "candidate.png",
            sha256="2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
            size_bytes=3,
        )


def test_local_materializer_rejects_candidate_root_quota_before_copy(tmp_path: Path) -> None:
    """候选声明总量超过 runtime 配额时必须在读取 Blob 前失败。"""
    from backend.visual_candidates import MAX_CANDIDATE_FILE_BYTES, MAX_CANDIDATE_ROOT_BYTES

    candidates = [
        {
            "meme_id": f"candidate-{index}",
            "image_sha256": f"{index + 1:064x}",
            "size_bytes": MAX_CANDIDATE_FILE_BYTES,
            "score": 0.5,
            "relative_path": f"candidate-{index:02d}.png",
            "context": {},
        }
        for index in range(MAX_CANDIDATE_ROOT_BYTES // MAX_CANDIDATE_FILE_BYTES + 1)
    ]
    snapshot = build_visual_match_snapshot(
        query_meme_id="query",
        image_sha256="a" * 64,
        model="dinov2_vitb14",
        dimensions=768,
        preprocess_version="dinov2-v1-gif-first-frame",
        candidates=candidates,
        matched_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    context = TrustedWorkspaceContext("task", "attempt", "local", visual_match_snapshot=snapshot)
    resolved = type("Resolved", (), {"candidate_root": tmp_path / "candidates" / "task"})()

    with pytest.raises(VisualCandidateMaterializationError):
        materialize_local_candidates(object(), context, resolved)
