"""视觉向量 repository 的身份、输入校验和候选资格回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.database import DatabaseError, VISUAL_EMBEDDING_DIMENSIONS, VisualEmbeddingRepository, validate_visual_vector
from backend.persistence.models import ScopeContext
from backend.persistence.repositories.visual_embeddings import VisualEmbeddingRepository as CanonicalVisualEmbeddingRepository
from backend.persistence.repositories.visual_embeddings import validate_visual_vector as canonical_validate_visual_vector


def _vector(value: float, *, dimensions: int = VISUAL_EMBEDDING_DIMENSIONS) -> list[float]:
    """构造固定维度的稀疏视觉向量，避免测试依赖真实 pgvector。"""
    vector = [0.0] * dimensions
    vector[0] = value
    return vector


def test_facade_exports_canonical_visual_repository_and_validator() -> None:
    """旧 facade 与 canonical 模块必须解析为同一类和同一校验函数。"""
    assert VisualEmbeddingRepository is CanonicalVisualEmbeddingRepository
    assert validate_visual_vector is canonical_validate_visual_vector


def test_validate_visual_vector_keeps_normalization_and_fail_closed_errors() -> None:
    """视觉向量继续归一化，并拒绝维度、有限性和范数错误。"""
    normalized = validate_visual_vector([3.0, 4.0], dimensions=2)
    assert normalized == [0.6, 0.8]

    with pytest.raises(DatabaseError, match="visual_embedding_dimensions_mismatch"):
        validate_visual_vector([1.0], dimensions=2)
    with pytest.raises(DatabaseError, match="visual_embedding_non_finite"):
        validate_visual_vector([float("nan")], dimensions=1)
    with pytest.raises(DatabaseError, match="visual_embedding_zero_norm"):
        validate_visual_vector([0.0], dimensions=1)


def test_upsert_checks_model_space_sha_and_scope_before_write() -> None:
    """upsert 必须校验模型空间、图片 SHA，并把新行绑定到当前 scope。"""
    meme = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"), sha256="a" * 64)

    class FakeSession:
        """为 upsert 提供最小的 Meme 查询和写入替身。"""

        def __init__(self, record):
            self.record = record
            self.scalar_calls = 0
            self.added = None
            self.flushed = False

        def scalar(self, _statement):
            self.scalar_calls += 1
            return self.record if self.scalar_calls == 1 else None

        def add(self, record):
            self.added = record

        def flush(self):
            self.flushed = True

    session = FakeSession(meme)
    repository = VisualEmbeddingRepository(session, ScopeContext("scope-a"))
    row = repository.upsert(
        meme.id,
        model="visual-model",
        preprocess_version="preprocess-v1",
        image_sha256=meme.sha256,
        embedding=[3.0, 4.0],
        dimensions=2,
    )
    assert row.scope_id == "scope-a"
    assert row.meme_id == meme.id
    assert row.embedding == [0.6, 0.8]
    assert session.added is row
    assert session.flushed is True

    with pytest.raises(DatabaseError, match="visual_model_not_configured"):
        repository.upsert(meme.id, model="", preprocess_version="v1", image_sha256=meme.sha256, embedding=[1.0], dimensions=1)
    with pytest.raises(DatabaseError, match="visual_embedding_sha256_invalid"):
        repository.upsert(meme.id, model="visual-model", preprocess_version="v1", image_sha256="bad", embedding=[1.0], dimensions=1)

    mismatched = FakeSession(SimpleNamespace(id=meme.id, sha256="b" * 64))
    with pytest.raises(DatabaseError, match="visual_embedding_sha256_mismatch"):
        VisualEmbeddingRepository(mismatched, ScopeContext("scope-a")).upsert(
            meme.id,
            model="visual-model",
            preprocess_version="v1",
            image_sha256="a" * 64,
            embedding=[1.0],
            dimensions=1,
        )


def test_match_filters_unready_provenance_and_stably_sorts_scores() -> None:
    """match 只保留当前图片 SHA 的 Agent-ready 候选并稳定排序。"""
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    unready_id = UUID("00000000-0000-0000-0000-000000000003")

    def meme(identifier: UUID, *, ready: bool, sha256: str = "a" * 64):
        """构造最小匹配候选，控制 Agent provenance 和图片 SHA。"""
        return SimpleNamespace(
            id=identifier,
            sha256=sha256,
            context_status="ready" if ready else "partial",
            provenance={
                "agent_context": {
                    "task_id": "task-1",
                    "model": "agent-model",
                    "skill_hash": "skill-hash",
                    "completed_at": "2026-01-01T00:00:00Z",
                    "image_sha256": sha256,
                }
            }
            if ready
            else {},
        )

    rows = [
        (SimpleNamespace(), meme(second_id, ready=True), 0.5),
        (SimpleNamespace(), meme(unready_id, ready=False), 0.99),
        (SimpleNamespace(), meme(first_id, ready=True), 0.5),
    ]

    class FakeResult:
        """返回预构造候选，避免测试依赖 PostgreSQL/pgvector 服务。"""

        def all(self):
            return rows

    class FakeSession:
        """记录查询但不执行数据库方言的最小 Session 替身。"""

        def __init__(self):
            self.statement = None

        def execute(self, statement):
            self.statement = statement
            return FakeResult()

    session = FakeSession()
    repository = VisualEmbeddingRepository(session, ScopeContext("scope-a"))
    result = repository.match(_vector(1.0), model="visual-model", preprocess_version="v1", limit=3)

    assert [meme_record.id for _embedding, meme_record, _score in result] == [first_id, second_id]
    assert [score for _embedding, _meme_record, score in result] == [0.5, 0.5]
    assert session.statement is not None


def test_agent_ready_requires_current_image_provenance() -> None:
    """候选语境必须完整且 provenance SHA 必须绑定当前图片。"""
    repository = object.__new__(VisualEmbeddingRepository)
    meme = SimpleNamespace(
        sha256="a" * 64,
        context_status="ready",
        provenance={
            "agent_context": {
                "task_id": "task-1",
                "model": "agent-model",
                "skill_hash": "skill-hash",
                "completed_at": "2026-01-01T00:00:00Z",
                "image_sha256": "a" * 64,
            }
        },
    )
    assert repository.agent_ready(meme) is True
    meme.provenance["agent_context"]["image_sha256"] = "b" * 64
    assert repository.agent_ready(meme) is False
