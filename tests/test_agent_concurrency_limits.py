"""共享 Agent 并发边界在各执行层的一致性测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import AGENT_BACKPRESSURE_DEFAULT, Settings
from backend.database import DatabaseError, _validate_lane_capacities
from backend.image_processing import ImageProcessingWorker
from backend.opencode import OpenCodeRunner
from backend.pg_services import PostgresTaskService, PostgresTaskWorkerManager
from executor.agent_limits import validate_agent_concurrency
from executor.server import _env_agent_backpressure, _env_agent_concurrency


def test_postgres_task_layers_preserve_configured_large_capacity() -> None:
    """PostgreSQL manager 与 scope facade 保留配置值，不把并发静默截断。"""
    configured = 128
    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = PostgresTaskWorkerManager(
            object(),
            agent_concurrency=configured,
            scope_concurrency=configured,
            agent_backpressure=configured,
            executor=executor,
        )
        assert manager.agent_concurrency == configured
        assert manager.agent_scope_concurrency == configured
        assert manager.agent_backpressure == configured

    with ThreadPoolExecutor(max_workers=1) as executor:
        service = PostgresTaskService(
            object(),
            agent_concurrency=configured,
            scope_concurrency=configured,
            agent_backpressure=configured,
            executor=executor,
        )
        assert service.agent_concurrency == configured
        assert service.agent_scope_concurrency == configured
        assert service.agent_backpressure == configured


def test_task_layers_reject_concurrency_above_backpressure() -> None:
    """并发超过资源背压或全局预算时显式失败，而不是收敛到另一个值。"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ValueError, match="agent_concurrency_exceeds_backpressure"):
            PostgresTaskWorkerManager(object(), agent_concurrency=81, executor=executor)
        with pytest.raises(ValueError, match="agent_concurrency_exceeds_backpressure"):
            PostgresTaskService(object(), agent_concurrency=81, executor=executor)
        with pytest.raises(ValueError, match="agent_concurrency_exceeds_backpressure"):
            PostgresTaskService(object(), agent_concurrency=1, scope_concurrency=2, executor=executor)


def test_database_lane_capacity_preserves_large_value_without_old_cap() -> None:
    """数据库公平 claim 保留大容量，并拒绝非法容量而不是静默截断。"""
    assert _validate_lane_capacities(256, 256) == (256, 256)
    with pytest.raises(DatabaseError, match="agent_claim_config_invalid"):
        _validate_lane_capacities(501, 501)


def test_opencode_and_image_workers_preserve_large_configured_capacity(tmp_path: Path) -> None:
    """OpenCode runner 与图片控制面沿用较大的配置值。"""
    configured = 128
    settings = Settings(
        _env_file=None,
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        opencode_concurrency=configured,
        agent_backpressure=configured,
    )
    runner = OpenCodeRunner(settings)
    try:
        assert runner.concurrency == configured
    finally:
        runner.shutdown()

    worker = ImageProcessingWorker(
        object(),
        scope_id="local",
        task_service=SimpleNamespace(agent_concurrency=configured, agent_scope_concurrency=1, agent_backpressure=configured),
        max_workers=configured,
    )
    try:
        assert worker.executor._max_workers == configured
    finally:
        worker.shutdown()


def test_executor_environment_parser_preserves_large_value_and_rejects_budget_overrun(monkeypatch) -> None:
    """独立 executor 接受较大配置，并拒绝超过背压预算的值。"""
    configured = 128
    monkeypatch.setenv("MEMEMEOW_AGENT_BACKPRESSURE", str(configured))
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(configured))
    backpressure = _env_agent_backpressure("MEMEMEOW_AGENT_BACKPRESSURE", AGENT_BACKPRESSURE_DEFAULT)
    assert _env_agent_concurrency("MEMEMEOW_OPENCODE_CONCURRENCY", 1, backpressure=backpressure) == configured
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(configured + 1))
    with pytest.raises(ValueError, match="agent_concurrency_exceeds_backpressure"):
        _env_agent_concurrency("MEMEMEOW_OPENCODE_CONCURRENCY", 1, backpressure=backpressure)


def test_agent_concurrency_safety_limit_is_explicit() -> None:
    """没有背压上下文时也拒绝超过公共资源安全上限的整数。"""
    with pytest.raises(ValueError, match="agent_concurrency_safety_limit"):
        validate_agent_concurrency(501)
