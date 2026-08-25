"""持久化 engine、事务单元、Repository、资源装配和旧 facade 的边界契约测试。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend import database
from backend.persistence import engine as persistence_engine
from backend.persistence import resources as persistence_resources
from backend.persistence import unit_of_work as persistence_unit_of_work
from backend.persistence.models import Scope, ScopeContext
from backend.persistence.repositories import collections as persistence_collections
from backend.persistence.repositories import memes as persistence_memes
from backend.persistence.repositories import search as persistence_search
from backend.persistence.repositories import visual_embeddings as persistence_visual_embeddings


def test_persistence_facade_reexports_one_implementation_source() -> None:
    """旧 facade 的 engine、事务和资源符号必须指向各自新模块的同一对象。"""
    engine_names = (
        "database_url_from_env",
        "create_engine_for_url",
        "create_engine_for_settings",
        "ensure_optional_control_schema",
        "check_database",
        "initialize_local",
    )
    for name in engine_names:
        assert getattr(database, name) is getattr(persistence_engine, name)
    assert database.DatabaseError is persistence_engine.DatabaseError
    assert database.UnitOfWork is persistence_unit_of_work.UnitOfWork
    assert database.DataEnvironment is persistence_resources.DataEnvironment
    assert database.DatabaseResources is persistence_resources.DatabaseResources
    assert database.MemeRepository is persistence_memes.MemeRepository
    assert database.CollectionRepository is persistence_collections.CollectionRepository
    assert database.SearchRepository is persistence_search.SearchRepository
    assert database.VisualEmbeddingRepository is persistence_visual_embeddings.VisualEmbeddingRepository
    assert database.validate_visual_vector is persistence_visual_embeddings.validate_visual_vector
    assert database.SCOPE_LOCAL == persistence_engine.SCOPE_LOCAL
    assert database.CURRENT_SCHEMA_REVISION == persistence_engine.CURRENT_SCHEMA_REVISION


def test_persistence_runtime_modules_have_no_top_level_facade_import() -> None:
    """新边界只允许在资源实际组装时延迟解析 facade，模块导入不能形成循环。"""
    for module in (persistence_engine, persistence_unit_of_work, persistence_resources, persistence_memes, persistence_collections, persistence_search, persistence_visual_embeddings):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_facade_imports = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "backend.database"
        }
        assert not top_level_facade_imports
    database_source = Path(database.__file__).read_text(encoding="utf-8")
    assert "class MemeRepository" not in database_source
    assert "class CollectionRepository" not in database_source
    assert "class SearchRepository" not in database_source
    assert "class VisualEmbeddingRepository" not in database_source
    assert "def validate_visual_vector" not in database_source
    assert "class TaskRepository" in database_source
    assert "class ReverseImageUsageRepository" in database_source
    assert "class BlobStore" in database_source
    assert "class StorageCoordinator" in database_source
    assert "from backend.persistence.repositories.search import SearchRepository" in database_source


def test_search_repository_package_exports_one_canonical_class() -> None:
    """Repository 子包和 facade 必须共同指向唯一 SearchRepository 实现。"""
    from backend.persistence.repositories import SearchRepository

    assert SearchRepository is persistence_search.SearchRepository
    source = Path(persistence_search.__file__).read_text(encoding="utf-8")
    assert source.count("class SearchRepository:") == 1


def test_unit_of_work_keeps_commit_and_rollback_lifecycle() -> None:
    """UnitOfWork 成功提交、异常回滚并在两条路径结束事务。"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (value INTEGER NOT NULL)"))
    factory = sessionmaker(engine, expire_on_commit=False, class_=Session)

    with persistence_unit_of_work.UnitOfWork(factory, ScopeContext("test")) as unit:
        unit.session.execute(text("INSERT INTO events(value) VALUES (1)"))
    with Session(engine) as session:
        assert session.scalar(text("SELECT count(*) FROM events")) == 1

    with pytest.raises(RuntimeError, match="rollback"):
        with persistence_unit_of_work.UnitOfWork(factory, ScopeContext("test")) as unit:
            unit.session.execute(text("INSERT INTO events(value) VALUES (2)"))
            raise RuntimeError("rollback")
    with Session(engine) as session:
        assert session.scalar(text("SELECT count(*) FROM events")) == 1


def test_data_environment_shares_one_scope_bound_session() -> None:
    """DataEnvironment 的全部 repository 必须共享同一个 UnitOfWork Session。"""
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(engine, expire_on_commit=False, class_=Session)
    with persistence_resources.DataEnvironment(factory, ScopeContext("scope-a")) as environment:
        repositories = (
            environment.memes,
            environment.collections,
            environment.search,
            environment.visual,
            environment.tasks,
            environment.reverse_image_usage,
            environment.callback_requests,
        )
        assert environment.visual_embeddings is environment.visual
        assert {id(repository.session) for repository in repositories} == {id(environment.uow.session)}


def test_database_resources_preserves_scope_storage_and_preflight_boundaries(monkeypatch, tmp_path) -> None:
    """资源装配继续读取数据库 namespace，并把预检委托给原 StorageCoordinator。"""
    engine = create_engine("sqlite:///:memory:")
    Scope.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([Scope(id="local"), Scope(id="other")])
        session.commit()

    monkeypatch.setattr(persistence_resources, "ensure_optional_control_schema", lambda _engine: None)

    class FakeBlobStore:
        """记录资源装配参数的文件存储替身。"""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.scope = kwargs["scope"]

    class FakeStorageCoordinator:
        """记录 flat preflight scope 的协调器替身。"""

        def __init__(self, resources, *, scope_id):
            self.resources = resources
            self.scope_id = scope_id

        def flat_preflight(self):
            return {"scope_id": self.scope_id}

    monkeypatch.setattr(database, "BlobStore", FakeBlobStore)
    monkeypatch.setattr(database, "StorageCoordinator", FakeStorageCoordinator)
    resources = persistence_resources.DatabaseResources(
        engine,
        image_root=tmp_path / "images",
        data_root=tmp_path / "data",
    )
    assert resources.blob_store.scope.scope_id == "local"
    assert resources.blob_store.kwargs["local"] is True
    other_store = resources.blob_store_for_scope("other")
    assert other_store.scope.scope_id == "other"
    assert other_store.kwargs["local"] is False
    assert resources.flat_preflight("other") == {"scope_id": "other"}
    with pytest.raises(persistence_engine.DatabaseError, match="scope_not_found"):
        resources.blob_store_for_scope("missing")


def test_database_resources_rejects_missing_scope_before_environment_creation() -> None:
    """DatabaseResources.environment 缺失 scope 时必须 fail-closed。"""
    resources = object.__new__(persistence_resources.DatabaseResources)
    with pytest.raises(persistence_engine.DatabaseError, match="scope_required"):
        resources.environment()
