"""文件一致性职责域的 BlobStore、状态机和兼容边界契约测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.database import BlobStore as LegacyBlobStore
from backend.database import DatabaseError, ScopeContext, StorageCoordinator as LegacyStorageCoordinator
from backend.persistence.models import StorageOperation
from backend.persistence.storage import BlobStore, StorageCoordinator


def test_storage_classes_have_one_canonical_module_and_legacy_identity() -> None:
    """BlobStore 与 StorageCoordinator 的旧路径必须复用 canonical 类对象。"""
    assert LegacyBlobStore is BlobStore
    assert LegacyStorageCoordinator is StorageCoordinator
    assert BlobStore.__module__ == "backend.persistence.storage"
    assert StorageCoordinator.__module__ == "backend.persistence.storage"


def test_blob_store_stages_moves_and_rejects_target_overwrite(tmp_path: Path) -> None:
    """暂存、落位和不覆盖移动必须保持源/目标文件事实。"""
    store = BlobStore(root=tmp_path / "images", scope=ScopeContext("local"), local=True)
    token = uuid4()
    staged = store.stage_bytes(b"source", token=token)
    assert store.exists_with_identity(staged, sha256="".join([])) is False
    store.link_move(staged, "source.png")
    (store.root / "target.png").write_bytes(b"winner")

    with pytest.raises(DatabaseError, match="target_exists"):
        store.link_move("source.png", "target.png")

    assert (store.root / "source.png").read_bytes() == b"source"
    assert (store.root / "target.png").read_bytes() == b"winner"


def test_blob_store_quarantine_and_identity_checks_are_scope_bound(tmp_path: Path) -> None:
    """隔离对象只能通过内部 key 访问，SHA/size 不匹配时必须返回 false。"""
    store = BlobStore(root=tmp_path / "images", scope=ScopeContext("local"), local=True)
    payload = b"quarantine-me"
    staged = store.stage_bytes(payload, token=uuid4())
    store.link_move(staged, "delete-me.png")
    digest = __import__("hashlib").sha256(payload).hexdigest()

    assert store.exists_with_identity("delete-me.png", sha256=digest, size_bytes=len(payload))
    assert not store.exists_with_identity("delete-me.png", sha256="0" * 64, size_bytes=len(payload))
    quarantine_key = store.quarantine("delete-me.png", token=uuid4())
    assert quarantine_key.startswith(".quarantine/")
    assert not (store.root / "delete-me.png").exists()
    assert store.exists_with_identity(quarantine_key, sha256=digest, size_bytes=len(payload))

    store.unlink(quarantine_key)
    assert not store.exists_with_identity(quarantine_key)
    with pytest.raises(DatabaseError, match="internal_storage_key"):
        store.resolve(quarantine_key, must_exist=False)


def test_blob_store_rejects_symlink_and_root_escape(tmp_path: Path) -> None:
    """业务对象不能通过符号链接或相对路径穿越访问外部文件。"""
    root = tmp_path / "images"
    store = BlobStore(root=root, scope=ScopeContext("local"), local=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (root / "link.png").symlink_to(outside)

    with pytest.raises(DatabaseError, match="symlink"):
        store.resolve("link.png", must_exist=False)
    with pytest.raises(DatabaseError):
        store.resolve("../outside", must_exist=False)
    with pytest.raises(DatabaseError, match="path_forbidden"):
        store.relative(outside)


def test_storage_operation_schema_retains_durable_and_fencing_facts() -> None:
    """storage_operations 模型必须保留恢复和任务 fencing 所需字段/约束。"""
    table = StorageOperation.__table__
    assert {
        "scope_id",
        "meme_id",
        "operation_token",
        "source_key",
        "target_key",
        "staging_key",
        "thumbnail_keys",
        "before_sha256",
        "after_sha256",
        "before_size",
        "after_size",
        "expected_revision",
        "claim_generation",
        "attempt",
        "task_id",
        "expected_title_fingerprint",
        "status",
        "error",
    } <= {column.name for column in table.columns}
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_storage_operation_status",
        "ck_storage_operation_type",
        "ck_storage_operation_expected_revision",
        "ck_storage_operation_claim_generation",
        "ck_storage_operation_attempt",
        "ck_storage_operation_title_fingerprint",
    }


def test_storage_coordinator_status_machine_is_fail_closed() -> None:
    """状态机只允许 durable operation 的合法推进，非法回退必须报错。"""
    class SessionSpy:
        """记录状态落盘时是否刷新事务 Session。"""

        flush_count = 0

        def flush(self) -> None:
            """记录一次状态刷新。"""
            self.flush_count += 1

    session = SessionSpy()
    coordinator = object.__new__(StorageCoordinator)
    coordinator._session = session
    operation = SimpleNamespace(status="prepared", error=None, updated_at=None)

    coordinator._set_status(operation, "file_applied")
    coordinator._set_status(operation, "completed")
    assert operation.status == "completed"
    assert session.flush_count == 2

    with pytest.raises(DatabaseError, match="invalid_storage_transition"):
        coordinator._set_status(operation, "prepared")


def test_storage_recovery_code_retains_blocked_unknown_and_skip_locked_guards() -> None:
    """恢复实现必须保留未知类型、歧义和并发恢复的 fail-closed 关键词。"""
    source = Path(StorageCoordinator.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    assert "skip_locked=True" in source
    assert '"blocked"' in source
    assert "storage_operation_unknown_type" in source
    assert "rename_recovery_ambiguous" in source
    assert "delete_recovery_ambiguous" in source
