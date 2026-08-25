"""PostgreSQL 应用服务 facade 的模块边界与兼容导出契约测试。"""

from __future__ import annotations

import ast
from pathlib import Path

from backend import pg_services
from backend.services import metadata, search, tasks, worker_manager


def test_pg_services_exports_canonical_service_objects() -> None:
    """旧 facade 与四个 canonical 模块必须返回同一组 class 对象。"""
    assert pg_services.PostgresMetadataService is metadata.PostgresMetadataService
    assert pg_services.PostgresSearchService is search.PostgresSearchService
    assert pg_services.PostgresTaskService is tasks.PostgresTaskService
    assert pg_services.PostgresTaskWorkerManager is worker_manager.PostgresTaskWorkerManager
    assert metadata.PostgresMetadataService.__module__ == "backend.services.metadata"
    assert search.PostgresSearchService.__module__ == "backend.services.search"
    assert tasks.PostgresTaskService.__module__ == "backend.services.tasks"
    assert worker_manager.PostgresTaskWorkerManager.__module__ == "backend.services.worker_manager"


def test_service_modules_have_one_implementation_and_no_facade_import() -> None:
    """canonical service 不得重复实现或反向导入兼容 facade。"""
    modules = (metadata, search, tasks, worker_manager)
    symbols = (
        (metadata, "PostgresMetadataService"),
        (search, "PostgresSearchService"),
        (tasks, "PostgresTaskService"),
        (worker_manager, "PostgresTaskWorkerManager"),
    )
    for module, symbol in symbols:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert source.count(f"class {symbol}:") == 1
        tree = ast.parse(source)
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "backend.pg_services"
            for node in tree.body
        )
        assert "from backend.pg_services" not in source

    facade_source = Path(pg_services.__file__).read_text(encoding="utf-8")
    facade_tree = ast.parse(facade_source)
    assert not any(isinstance(node, ast.ClassDef) for node in facade_tree.body)
    assert set(pg_services.__all__) == {
        "PostgresMetadataService",
        "PostgresSearchService",
        "PostgresTaskService",
        "PostgresTaskWorkerManager",
    }


def test_service_dependency_direction_is_explicit() -> None:
    """search 到 metadata、tasks 到 worker 的依赖必须保持单向。"""
    search_source = Path(search.__file__).read_text(encoding="utf-8")
    task_source = Path(tasks.__file__).read_text(encoding="utf-8")
    worker_source = Path(worker_manager.__file__).read_text(encoding="utf-8")
    assert "from backend.services.metadata import PostgresMetadataService" in search_source
    assert "from backend.services.worker_manager import PostgresTaskWorkerManager" in task_source
    assert "backend.services.tasks" not in worker_source
    assert "backend.services.search" not in task_source
    assert 'logging.getLogger("backend.pg_services")' in search_source
    assert 'logging.getLogger("backend.pg_services")' in task_source
    assert 'logging.getLogger("backend.pg_services")' in worker_source
