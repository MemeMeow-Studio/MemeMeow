"""共享 Agent 并发边界在各执行层的一致性测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import AGENT_BACKPRESSURE_DEFAULT, Settings
from backend.database import DatabaseError, _validate_lane_capacities, validate_lane_resource_concurrency, validate_lane_resource_key
from backend.image_processing import ImageProcessingWorker
from backend.opencode import OpenCodeRunner
from backend.pg_services import PostgresTaskService, PostgresTaskWorkerManager
from executor.agent_limits import validate_agent_backpressure, validate_agent_concurrency, validate_agent_concurrency_at_most
from executor.server import _env_agent_backpressure, _env_agent_concurrency


def test_postgres_task_layers_preserve_configured_large_capacity() -> None:
    """PostgreSQL manager 与 scope facade 接受大运行容量，不依赖旧队列背压。"""
    configured = 500
    scope_limit = 10
    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = PostgresTaskWorkerManager(
            object(),
            agent_concurrency=configured,
            scope_concurrency=scope_limit,
            agent_backpressure=80,
            executor=executor,
        )
        assert manager.agent_concurrency == configured
        assert manager.agent_scope_concurrency == scope_limit

    with ThreadPoolExecutor(max_workers=1) as executor:
        service = PostgresTaskService(
            object(),
            agent_concurrency=configured,
            scope_concurrency=scope_limit,
            agent_backpressure=80,
            executor=executor,
        )
        assert service.agent_concurrency == configured
        assert service.agent_scope_concurrency == scope_limit


def test_task_layers_enforce_scope_relation_without_legacy_backpressure() -> None:
    """任务层忽略旧背压字段，但仍拒绝 scope 运行容量超过全局容量。"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ValueError, match="agent_scope_concurrency_exceeds_global"):
            PostgresTaskWorkerManager(object(), agent_concurrency=500, scope_concurrency=501, agent_backpressure=80, executor=executor)
        with pytest.raises(ValueError, match="agent_scope_concurrency_exceeds_global"):
            PostgresTaskService(object(), agent_concurrency=500, scope_concurrency=501, agent_backpressure=80, executor=executor)
        with pytest.raises(ValueError, match="agent_scope_concurrency_exceeds_global"):
            PostgresTaskService(object(), agent_concurrency=1, scope_concurrency=2, executor=executor)


def test_database_lane_capacity_preserves_large_value_without_old_cap() -> None:
    """数据库公平 claim 保留大容量，并拒绝非法容量而不是静默截断。"""
    assert _validate_lane_capacities(1024, 1024) == (1024, 1024)
    with pytest.raises(DatabaseError, match="agent_claim_config_invalid"):
        _validate_lane_capacities(0, 0)


def test_resource_capacity_mapping_is_opaque_and_cannot_exceed_global() -> None:
    """资源映射只校验 key 和正整数关系，缺失项由调用方继承全局值。"""
    assert validate_lane_resource_concurrency({"free_series": 40, "luna_high": 60}, 500) == {"free_series": 40, "luna_high": 60}
    assert validate_lane_resource_concurrency(None, 500) == {}
    assert validate_lane_resource_key(None) == "__global__"
    with pytest.raises(ValueError, match="agent_resource_concurrency_invalid"):
        validate_lane_resource_concurrency({"model": 501}, 500)
    with pytest.raises(ValueError, match="agent_resource_key_duplicate"):
        validate_lane_resource_concurrency({"model": 1, " model ": 2}, 500)


def test_opencode_and_image_workers_preserve_large_configured_capacity(tmp_path: Path) -> None:
    """OpenCode runner 与图片控制面接受超过旧背压默认值的运行容量。"""
    configured = 500
    settings = Settings(
        _env_file=None,
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        opencode_concurrency=configured,
        agent_scope_concurrency=10,
    )
    runner = OpenCodeRunner(settings)
    try:
        assert runner.concurrency == configured
    finally:
        runner.shutdown()

    worker = ImageProcessingWorker(
        object(),
        scope_id="local",
        task_service=SimpleNamespace(agent_concurrency=configured, agent_scope_concurrency=1),
        max_workers=configured,
    )
    try:
        assert worker.executor._max_workers == configured
    finally:
        worker.shutdown()


def test_executor_environment_parser_ignores_legacy_backpressure(monkeypatch) -> None:
    """独立 executor 的运行容量不受旧背压配置限制。"""
    configured = 500
    monkeypatch.setenv("MEMEMEOW_AGENT_BACKPRESSURE", "80")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(configured))
    backpressure = _env_agent_backpressure("MEMEMEOW_AGENT_BACKPRESSURE", AGENT_BACKPRESSURE_DEFAULT)
    assert _env_agent_concurrency("MEMEMEOW_OPENCODE_CONCURRENCY", 1, backpressure=backpressure) == configured
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(configured + 1))
    assert _env_agent_concurrency("MEMEMEOW_OPENCODE_CONCURRENCY", 1, backpressure=backpressure) == configured + 1


def test_agent_capacity_has_no_shared_fixed_safety_limit() -> None:
    """公共核心接受大运行容量，层级关系由显式运行容量校验负责。"""
    assert validate_agent_backpressure(80) == 80
    assert validate_agent_concurrency(500, backpressure=80) == 500
    assert validate_agent_concurrency_at_most(10, 500) == 10
    with pytest.raises(ValueError, match="agent_scope_concurrency_exceeds_global"):
        validate_agent_concurrency_at_most(501, 500, error_code="agent_scope_concurrency_exceeds_global")
