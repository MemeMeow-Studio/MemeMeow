"""任务摘要批量装配 Agent 活跃度字段的 API 边界测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from api import _task_summary, get_task, list_tasks
from backend.opencode_activity import AgentActivity, OpenCodeActivityReader
from backend.tasks import TaskRecord


class FakeActivityReader:
    """记录批量调用并返回固定领域值的测试 reader。"""

    def __init__(self, values=None, error: Exception | None = None):
        self.values = values or {}
        self.error = error
        self.calls: list[list[str]] = []

    def read_many(self, task_ids):
        """模拟一次批量 SQLite 观测。"""
        self.calls.append(list(task_ids))
        if self.error:
            raise self.error
        return {task_id: self.values[task_id] for task_id in task_ids if task_id in self.values}


class FakeTasks:
    """为路由函数提供任务列表和详情快照。"""

    def __init__(self, records: list[TaskRecord]):
        self.records = records

    def list(self, **_kwargs):
        """返回固定任务页，不执行额外查询。"""
        return self.records, None

    def get(self, task_id: str):
        """按任务 ID 返回固定详情。"""
        return next((record for record in self.records if record.task_id == task_id), None)


def _request(records: list[TaskRecord], reader: FakeActivityReader):
    """构造只包含摘要装配依赖的伪 Request。"""
    metadata = SimpleNamespace(image_for_meme=lambda _meme_id: (None, Path("sample.png")))
    state = SimpleNamespace(tasks=FakeTasks(records), agent_activity=reader, metadata=metadata)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _request_with_settings(record: TaskRecord, reader: FakeActivityReader, settings: object):
    """构造带续跑 rollout 配置的任务详情请求。"""
    metadata = SimpleNamespace(image_for_meme=lambda _meme_id: (None, Path("sample.png")))
    state = SimpleNamespace(tasks=FakeTasks([record]), agent_activity=reader, metadata=metadata, settings=settings)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _context_record(task_id: str) -> TaskRecord:
    """创建带图片身份的语境任务快照。"""
    return TaskRecord(task_id=task_id, task_type="meme_context_generation", payload={"meme_id": f"meme-{task_id}"}, status="running")


def test_task_list_reads_activity_once_and_omits_non_context_fields():
    """任务列表对多个语境任务只批量读取一次，其他类型不带 Agent 字段。"""
    records = [_context_record("one"), _context_record("two"), TaskRecord(task_id="cache", task_type="cache_generation")]
    reader = FakeActivityReader({"one": AgentActivity(3, True, "2026-08-13T12:23:57Z"), "two": AgentActivity(4, False, "2026-08-13T12:24:57Z")})
    request = _request(records, reader)

    response = asyncio.run(list_tasks(request, status=[], task_type=[], cursor=None, limit=50))

    assert reader.calls == [["one", "two"]]
    assert response["items"][0]["agent_completed_turns"] == 3
    assert response["items"][0]["agent_turn_running"] is True
    assert response["items"][1]["agent_turn_running"] is False
    assert "agent_completed_turns" not in response["items"][2]


def test_task_detail_reuses_single_activity_boundary():
    """单任务详情复用同一 reader 边界并返回完整字段。"""
    record = _context_record("detail")
    reader = FakeActivityReader({"detail": AgentActivity(18, True, "2026-08-13T12:23:57Z")})
    request = _request([record], reader)

    response = asyncio.run(get_task(request, "detail"))

    assert reader.calls == [["detail"]]
    assert response["agent_completed_turns"] == 18
    assert response["agent_turn_running"] is True
    assert response["agent_last_activity_at"].endswith("Z")


def test_task_summary_masks_resume_when_rollout_is_disabled_or_budget_exhausted():
    """任务详情不能把已持久化的 session 在关闭开关或额度耗尽后继续报告为可恢复。"""
    disabled = _context_record("resume-disabled")
    disabled.resume_available = True
    disabled.resume_reason = "agent_provider_server_error"
    disabled.session_id = "session-resume-disabled"
    disabled.executor_attempt_id = "attempt-resume-disabled"
    disabled_request = _request_with_settings(disabled, FakeActivityReader(), SimpleNamespace(agent_resume_enabled=False, agent_resume_max_attempts=2))
    disabled_response = _task_summary(disabled_request, disabled)
    assert disabled_response["resume_available"] is False
    assert disabled_response["resume_reason"] == "resume_disabled"

    exhausted = _context_record("resume-exhausted")
    exhausted.resume_available = True
    exhausted.resume_attempts = 2
    exhausted.session_id = "session-resume-exhausted"
    exhausted.executor_attempt_id = "attempt-resume-exhausted"
    exhausted_request = _request_with_settings(exhausted, FakeActivityReader(), SimpleNamespace(agent_resume_enabled=True, agent_resume_max_attempts=2))
    exhausted_response = _task_summary(exhausted_request, exhausted)
    assert exhausted_response["resume_available"] is False
    assert exhausted_response["resume_reason"] == "resume_budget_exhausted"


def test_task_summary_keeps_queued_auto_resume_visible_within_budget():
    """尚未耗尽额度的排队任务仍显示可续跑，便于诊断自动恢复状态。"""
    record = _context_record("resume-queued")
    record.status = "queued"
    record.resume_available = True
    record.resume_attempts = 1
    record.session_id = "session-resume-queued"
    record.executor_attempt_id = "attempt-resume-queued"
    request = _request_with_settings(record, FakeActivityReader(), SimpleNamespace(agent_resume_enabled=True, agent_resume_max_attempts=2))
    response = _task_summary(request, record)
    assert response["status"] == "queued"
    assert response["resume_available"] is True


def test_task_summary_masks_resume_after_cumulative_timeout():
    """续跑累计时间超过上限后，即使尚未再次 claim 也不显示可恢复。"""
    record = _context_record("resume-timeout")
    record.resume_available = True
    record.resume_started_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    record.session_id = "session-resume-timeout"
    record.executor_attempt_id = "attempt-resume-timeout"
    request = _request_with_settings(record, FakeActivityReader(), SimpleNamespace(agent_resume_enabled=True, agent_resume_max_attempts=2, agent_resume_timeout_seconds=60))
    response = _task_summary(request, record)
    assert response["resume_available"] is False
    assert response["resume_reason"] == "resume_budget_exhausted"


def test_reader_error_keeps_task_api_successful_and_omits_all_activity():
    """reader 异常只会隐藏活动字段，不改变既有任务响应。"""
    record = _context_record("failed-reader")
    reader = FakeActivityReader(error=PermissionError("database unavailable"))
    request = _request([record], reader)

    response = asyncio.run(get_task(request, "failed-reader"))

    assert response["task_id"] == "failed-reader"
    assert response["status"] == "running"
    assert not {"agent_completed_turns", "agent_turn_running", "agent_last_activity_at"} & response.keys()


def test_incomplete_activity_is_not_exposed_as_partial_summary():
    """不完整活动值不会被误报为零轮或半个活动摘要。"""
    record = _context_record("partial")
    reader = FakeActivityReader({"partial": {"agent_completed_turns": 0, "agent_turn_running": False}})
    request = _request([record], reader)

    response = asyncio.run(get_task(request, "partial"))

    assert not {"agent_completed_turns", "agent_turn_running", "agent_last_activity_at"} & response.keys()


def test_task_summary_exposes_only_resume_diagnostics():
    """任务详情公开 session/attempt 和有限错误历史，不泄漏完整执行上下文。"""
    record = TaskRecord(
        task_id="resume-detail",
        task_type="meme_context_generation",
        payload={"meme_id": "meme-resume-detail", "prompt": "must not appear"},
        status="failed",
        resume_available=True,
        resume_reason="agent_provider_rate_limited",
        session_id="session-resume-detail",
        executor_attempt_id="attempt-resume-detail",
        resume_attempts=1,
        first_error={"error": "agent_provider_rate_limited", "message": "短暂失败"},
        error_history=[{"error": "agent_provider_rate_limited", "attempt": 1}],
    )
    response = asyncio.run(get_task(_request([record], FakeActivityReader()), record.task_id))

    assert response["resume_available"] is True
    assert response["session_id"] == "session-resume-detail"
    assert response["executor_attempt_id"] == "attempt-resume-detail"
    assert response["resume_attempts"] == 1
    assert response["first_error"]["error"] == "agent_provider_rate_limited"
    assert "prompt" not in response


def test_task_summary_fails_closed_for_invalid_resume_identifiers():
    """损坏的历史恢复标识不能在 API 中继续显示为可续跑。"""
    record = TaskRecord(
        task_id="resume-invalid",
        task_type="meme_context_generation",
        status="failed",
        resume_available=True,
        resume_reason="agent_provider_rate_limited",
        session_id="/runtime/opencode.db",
        executor_attempt_id="attempt-valid",
    )
    response = asyncio.run(get_task(_request([record], FakeActivityReader()), record.task_id))

    assert response["resume_available"] is False
    assert response["resume_reason"] == "session_not_resumable"
    assert response["session_id"] is None
    assert response["executor_attempt_id"] == "attempt-valid"


def test_fault_injected_reader_keeps_list_and_detail_successful(tmp_path: Path):
    """数据库缺失、schema 不兼容和临时锁忙时两个任务路由仍返回成功。"""
    runtimes: list[tuple[Path, sqlite3.Connection | None]] = [(tmp_path / "missing", None)]
    incompatible = tmp_path / "incompatible"
    incompatible.mkdir()
    connection = sqlite3.connect(incompatible / "opencode.db")
    connection.execute("CREATE TABLE session(id TEXT)")
    connection.commit()
    connection.close()
    runtimes.append((incompatible, None))

    locked = tmp_path / "locked"
    locked.mkdir()
    connection = sqlite3.connect(locked / "opencode.db")
    connection.executescript(
        """
        CREATE TABLE session(id TEXT PRIMARY KEY, title TEXT NOT NULL, time_created INTEGER NOT NULL);
        CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        """
    )
    connection.commit()
    connection.execute("BEGIN EXCLUSIVE")
    runtimes.append((locked, connection))

    try:
        for runtime, _writer in runtimes:
            record = _context_record(f"fault-{runtime.name}")
            reader = OpenCodeActivityReader(runtime, busy_timeout_ms=1)
            request = _request([record], reader)
            listed = asyncio.run(list_tasks(request, status=[], task_type=[], cursor=None, limit=50))
            detail = asyncio.run(get_task(request, record.task_id))
            assert listed["items"][0]["status"] == "running"
            assert detail["status"] == "running"
            assert not {"agent_completed_turns", "agent_turn_running", "agent_last_activity_at"} & detail.keys()
    finally:
        connection.rollback()
        connection.close()
