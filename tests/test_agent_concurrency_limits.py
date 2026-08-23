"""共享 Agent 并发边界在各执行层的一致性测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from backend.config import AGENT_BACKPRESSURE_DEFAULT, AGENT_CONCURRENCY_MAX, Settings
from backend.image_processing import ImageProcessingWorker
from backend.opencode import OpenCodeRunner
from backend.pg_services import PostgresTaskService, PostgresTaskWorkerManager
from executor.server import _env_int


def test_postgres_task_layers_share_forty_slot_boundary() -> None:
    """PostgreSQL manager 与 scope facade 都接受 40 并将过大值收敛到 40。"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = PostgresTaskWorkerManager(
            object(),
            agent_concurrency=AGENT_CONCURRENCY_MAX + 1,
            scope_concurrency=AGENT_CONCURRENCY_MAX + 1,
            executor=executor,
        )
        assert manager.agent_concurrency == AGENT_CONCURRENCY_MAX
        assert manager.agent_scope_concurrency == AGENT_CONCURRENCY_MAX
        assert manager.agent_backpressure == AGENT_BACKPRESSURE_DEFAULT

    with ThreadPoolExecutor(max_workers=1) as executor:
        service = PostgresTaskService(
            object(),
            agent_concurrency=AGENT_CONCURRENCY_MAX,
            scope_concurrency=AGENT_CONCURRENCY_MAX,
            executor=executor,
        )
        assert service.agent_concurrency == AGENT_CONCURRENCY_MAX
        assert service.agent_scope_concurrency == AGENT_CONCURRENCY_MAX
        assert service.agent_backpressure == AGENT_BACKPRESSURE_DEFAULT


def test_opencode_and_image_workers_share_forty_slot_boundary(tmp_path: Path) -> None:
    """OpenCode runner 与图片控制面不再保留旧的 8/4 并发截断。"""
    settings = Settings(
        _env_file=None,
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        opencode_concurrency=AGENT_CONCURRENCY_MAX,
    )
    runner = OpenCodeRunner(settings)
    try:
        assert runner.concurrency == AGENT_CONCURRENCY_MAX
    finally:
        runner.shutdown()

    worker = ImageProcessingWorker(
        object(),
        scope_id="local",
        task_service=SimpleNamespace(agent_concurrency=AGENT_CONCURRENCY_MAX, agent_scope_concurrency=1, agent_backpressure=AGENT_BACKPRESSURE_DEFAULT),
        max_workers=AGENT_CONCURRENCY_MAX + 1,
    )
    try:
        assert worker.executor._max_workers == AGENT_CONCURRENCY_MAX
    finally:
        worker.shutdown()


def test_executor_environment_parser_accepts_forty_and_clamps_above_boundary(monkeypatch) -> None:
    """独立 executor 快照接受 40，并将越界环境值收敛到公开最大值。"""
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(AGENT_CONCURRENCY_MAX))
    assert _env_int("MEMEMEOW_OPENCODE_CONCURRENCY", 1, 1, AGENT_CONCURRENCY_MAX) == AGENT_CONCURRENCY_MAX
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", str(AGENT_CONCURRENCY_MAX + 1))
    assert _env_int("MEMEMEOW_OPENCODE_CONCURRENCY", 1, 1, AGENT_CONCURRENCY_MAX) == AGENT_CONCURRENCY_MAX
