"""SearchRepository 的单一来源、错误码和向量排序回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.database import DatabaseError, EMBEDDING_DIMENSIONS, SearchRepository


def _vector(value: float, *, index: int = 0) -> list[float]:
    """构造固定维度的稀疏测试向量，避免依赖真实 pgvector。"""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = value
    return vector


def test_query_uses_only_incremental_source(monkeypatch) -> None:
    """query 只允许当前增量来源，旧 generation 不再作为运行时回退。"""
    repository = object.__new__(SearchRepository)
    vector = _vector(1.0)
    calls: list[str] = []

    monkeypatch.setattr(repository, "source_mode", lambda _model: "incremental")
    monkeypatch.setattr(repository, "query_incremental", lambda *_args: calls.append("incremental") or [(UUID(int=1), 1.0)])
    monkeypatch.setattr(repository, "_query_legacy_validated", lambda *_args: calls.append("legacy") or [])

    assert repository.query("model", vector) == [(UUID(int=1), 1.0)]
    assert calls == ["incremental"]

    calls.clear()
    monkeypatch.setattr(repository, "source_mode", lambda _model: "legacy")
    monkeypatch.setattr(repository, "_query_legacy_validated", lambda *_args: calls.append("legacy") or [(UUID(int=2), 0.5)])
    monkeypatch.setattr(repository, "query_incremental", lambda *_args: calls.append("incremental") or [])

    with pytest.raises(DatabaseError, match="cache_not_ready"):
        repository.query("model", vector)
    assert calls == []


def test_legacy_query_keeps_score_then_meme_id_order_and_limit() -> None:
    """旧 generation 查询按余弦分数和 Meme UUID 稳定排序并限制结果数。"""
    repository = object.__new__(SearchRepository)
    lower_id = UUID("00000000-0000-0000-0000-000000000001")
    higher_id = UUID("00000000-0000-0000-0000-000000000002")
    orthogonal_id = UUID("00000000-0000-0000-0000-000000000003")
    rows = [
        (SimpleNamespace(meme_id=higher_id, embedding=_vector(1.0)), None),
        (SimpleNamespace(meme_id=orthogonal_id, embedding=_vector(1.0, index=1)), None),
        (SimpleNamespace(meme_id=lower_id, embedding=_vector(1.0)), None),
    ]
    repository._legacy_rows = lambda _model: rows

    assert repository._query_legacy_validated("model", _vector(1.0), 2) == [
        (lower_id, 1.0),
        (higher_id, 1.0),
    ]


def test_incremental_query_returns_only_validated_meme_ids() -> None:
    """增量查询从行关联的 Meme ID 返回结果，并保留稳定排序。"""
    repository = object.__new__(SearchRepository)
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    repository._incremental_rows = lambda _model: [
        (SimpleNamespace(embedding=_vector(1.0)), SimpleNamespace(id=second_id)),
        (SimpleNamespace(embedding=_vector(1.0, index=1)), SimpleNamespace(id=first_id)),
    ]

    assert repository.query_incremental("model", _vector(1.0), 5) == [
        (second_id, 1.0),
        (first_id, 0.0),
    ]


def test_valid_text_embedding_ids_only_returns_requested_images_from_selected_source() -> None:
    """逐图状态只返回请求图片中通过当前来源校验的 ID，不能复用其它图片的缓存。"""
    repository = object.__new__(SearchRepository)
    ready_id = UUID("00000000-0000-0000-0000-000000000001")
    pending_id = UUID("00000000-0000-0000-0000-000000000002")
    ready_meme = SimpleNamespace(id=ready_id)
    pending_meme = SimpleNamespace(id=pending_id)
    repository.migration_state = lambda _model: SimpleNamespace(mode="incremental_only")
    repository._incremental_rows = lambda _model: [(SimpleNamespace(), ready_meme)]
    repository._legacy_rows = lambda _model: pytest.fail("不应读取未选中的 legacy 来源")

    assert repository.valid_text_embedding_ids("model", [ready_meme, pending_meme]) == {ready_id}


def test_query_rejects_invalid_dimensions_and_zero_norm() -> None:
    """输入维度和范数错误必须继续 fail-closed。"""
    repository = object.__new__(SearchRepository)
    with pytest.raises(DatabaseError, match="embedding_dimensions_mismatch"):
        repository.query("model", [1.0])

    repository.source_mode = lambda _model: "legacy"
    with pytest.raises(DatabaseError, match="embedding_zero_norm"):
        repository.query("model", _vector(0.0))
