# 图片库状态筛选使用真实 PostgreSQL 验证计数、分页和 scope 隔离。

import os
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from backend.image_library_status import image_status_condition
from backend.persistence.engine import create_engine_for_url
from backend.persistence.models import EMBEDDING_DIMENSIONS, MemeTextEmbedding, Scope, ScopeContext
from backend.persistence.resources import DataEnvironment
from backend.visual import VisualModelIdentity


@pytest.fixture
def library_environment():
    """在已有开发数据库的独立事务中创建测试 scope，退出时清理本次数据。"""
    url = os.environ.get("MEMEMEOW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置 MEMEMEOW_TEST_DATABASE_URL")
    engine = create_engine_for_url(url)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            factory = sessionmaker(bind=connection, expire_on_commit=False)
            scope = ScopeContext(f"test-library-status-{uuid4().hex}")
            with DataEnvironment(factory, scope) as environment:
                environment.uow.session.add(Scope(id=scope.scope_id))
                environment.uow.session.flush()
                yield environment
            transaction.rollback()
    finally:
        engine.dispose()


def add_image(environment: DataEnvironment, index: int, name: str, *, context: str, text: bool, visual: bool, stale_text: bool = False, stale_visual: bool = False):
    """保存带指定语境和实际向量记录的测试图片，供数据库状态查询验收。"""
    sha = f"{index:064x}"
    record = environment.memes.create(storage_key=f"{sha}.png", extension=".png", size_bytes=1, sha256=sha, context={}, provenance={}, display_name=name, status=context)
    if text:
        environment.uow.session.add(MemeTextEmbedding(
            scope_id=environment.scope.scope_id, meme_id=record.id, image_sha256=record.sha256,
            metadata_hash="f" * 64 if stale_text or not record.search_metadata_hash else record.search_metadata_hash,
            embedding_model_version="library-test-model", semantic_document=name,
            embedding=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1), status="ready",
        ))
    if visual:
        identity = VisualModelIdentity()
        environment.visual.upsert(record.id, model="historical-model" if stale_visual else identity.model, preprocess_version=identity.preprocess_version, dimensions=identity.dimensions, image_sha256=record.sha256, embedding=[1.0] + [0.0] * (identity.dimensions - 1))
    environment.uow.session.flush()
    return record


def test_status_filter_counts_pages_and_projection(library_environment: DataEnvironment):
    """验证六类状态与真实向量投影一致，搜索、排序和分页使用同一筛选条件。"""
    environment = library_environment
    definitions = [
        ("ready", "ready", True, True),
        ("context", "pending", True, True),
        ("text", "ready", False, True),
        ("visual", "ready", True, False),
        ("all", "pending", False, False),
        ("repair", "repair_required", True, True),
        ("stale-text", "ready", True, True),
        ("stale-visual", "ready", True, True),
    ]
    records = [add_image(environment, index, name, context=context, text=text, visual=visual, stale_text=name == "stale-text", stale_visual=name == "stale-visual") for index, (name, context, text, visual) in enumerate(definitions, start=1)]
    identity = VisualModelIdentity()
    text_ids = environment.search.valid_text_embedding_ids("library-test-model", records)
    visual_ids = environment.visual.ready_ids(records, **identity.as_dict())
    expected = {
        "all": {record.id for record in records},
        "ready": {record.id for record in records if record.context_status == "ready" and record.id in text_ids and record.id in visual_ids},
        "context_unready": {record.id for record in records if record.context_status != "ready"},
        "text_unready": {record.id for record in records if record.id not in text_ids},
        "visual_unready": {record.id for record in records if record.id not in visual_ids},
    }
    expected["unready"] = expected["all"] - expected["ready"]
    assert [len(expected[status]) for status in ("ready", "unready", "context_unready", "text_unready", "visual_unready")] == [1, 7, 3, 4, 3]
    for status, identifiers in expected.items():
        condition = image_status_condition(environment, status, embedding_model="library-test-model", visual_identity=identity)
        assert environment.memes.count(condition=condition) == len(identifiers)
        for sort in ("name_asc", "updated_desc"):
            first = environment.memes.list(condition=condition, sort=sort, page_size=2)
            pages = [record for page in range(1, 6) for record in environment.memes.list(condition=condition, sort=sort, page=page, page_size=2)]
            assert {record.id for record in pages} == identifiers
            assert len(pages) == len(identifiers)
            assert pages[:2] == first
        filtered = environment.memes.list(condition=condition, search="stale", page_size=2)
        assert environment.memes.count(condition=condition, search="stale") == len(filtered)
        assert {record.id for record in filtered} == identifiers & {records[6].id, records[7].id}
    other = ScopeContext(f"test-library-status-other-{uuid4().hex}")
    environment.uow.session.add(Scope(id=other.scope_id))
    environment.uow.session.flush()
    from backend.persistence.repositories.memes import MemeRepository

    assert MemeRepository(environment.uow.session, other).count() == 0


@pytest.mark.parametrize("page_size", [20, 50, 100, 200])
def test_page_sizes_and_upper_bound(library_environment: DataEnvironment, page_size: int):
    """验证四档数量、末页和 200 上限在真实数据库中生效。"""
    environment = library_environment
    for index in range(1, 202):
        add_image(environment, index, f"page-size-{index:03d}", context="pending", text=False, visual=False)
    first = environment.memes.list(page_size=page_size)
    second = environment.memes.list(page=2, page_size=page_size)
    assert len(first) == page_size
    assert len(second) == min(page_size, 201 - page_size)
    assert not {record.id for record in first} & {record.id for record in second}
    assert environment.memes.count() == 201
    assert len(environment.memes.list(page_size=500)) == 200
