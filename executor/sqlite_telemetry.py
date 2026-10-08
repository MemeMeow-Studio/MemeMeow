# Executor 的 OpenCode 只读观测，读取范围来自当前任务的可信执行绑定。
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from pydantic import BaseModel, ConfigDict

from runtime_telemetry import protected_content, record_content


class SavedContent(BaseModel):
    """读取固定 OpenCode 版本的结构化 JSON，保留来源提供的扩展字段。"""

    model_config = ConfigDict(extra="allow")
    role: str | None = None
    type: str | None = None
    time: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    modelID: str | None = None
    providerID: str | None = None


def read_saved_records(database: Path, *, directory: Path, session_id: str) -> list[dict[str, Any]]:
    """读取已绑定主 session 和相同目录的子 session；250ms 等待并限制查询时间。"""
    if not session_id:
        raise ValueError("sqlite_session_binding_missing")
    if not database.is_file() or database.is_symlink():
        raise ValueError("sqlite_database_missing_or_symlink")
    started = time.monotonic()
    with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(lambda: int(time.monotonic() - started > 0.25), 1000)
        connection.execute("BEGIN")
        root = connection.execute("SELECT parent_id, directory FROM session WHERE id = ?", (session_id,)).fetchone()
        if root is None or root[0] is not None or Path(root[1]).resolve() != directory.resolve():
            raise ValueError("sqlite_session_directory_mismatch")
        sessions = connection.execute("""
            WITH RECURSIVE bound(id, directory) AS (
                SELECT id, directory FROM session WHERE id = ?
                UNION SELECT session.id, session.directory FROM session JOIN bound ON session.parent_id = bound.id
            ) SELECT id, directory FROM bound
        """, (session_id,)).fetchall()
        if any(Path(row[1]).resolve() != directory.resolve() for row in sessions):
            raise ValueError("sqlite_child_session_directory_mismatch")
        identifiers = [row[0] for row in sessions]
        placeholders = ",".join("?" for identifier in identifiers)
        records = []
        for table in ("message", "part"):
            rows = connection.execute(f"SELECT id, session_id, time_created, time_updated, data FROM {table} WHERE session_id IN ({placeholders}) ORDER BY time_updated, id", identifiers)
            for identifier, bound_session, created, updated, data in rows:
                content = SavedContent.model_validate_json(data).model_dump(exclude_none=True)
                records.append({"kind": table, "id": identifier, "session_id": bound_session, "created_ms": created, "updated_ms": updated, "data": content})
        connection.rollback()
        return records


class SQLiteTelemetry:
    """后台采集当前 attempt 的 SQLite 更新，恢复读取时建立终态基线。"""

    def __init__(self, *, database: Path, directory: Path, session: Callable[[], str | None], attributes: dict[str, Any], context: Context, started_at: float, secrets: tuple[str, ...]):
        self.database = database
        self.directory = directory
        self.session = session
        self.attributes = attributes
        self.context = context
        self.started_at = started_at
        self.secrets = secrets
        self.stop = Event()
        self.thread = Thread(target=self._loop, name="opencode-sqlite-telemetry", daemon=True)
        self.versions: dict[tuple[str, str], tuple[int, str]] = {}
        self.terminals: set[tuple[str, str]] = set()
        self.baseline = True
        self.failed_since: float | None = None
        self.last_success = 0.0
        meter = metrics.get_meter("mememeow.agent")
        self.success_time = meter.create_gauge("agent_sqlite_last_success", unit="s")
        self.failures = meter.create_counter("agent_sqlite_read_errors", unit="{error}")
        self.active_failures = meter.create_up_down_counter("agent_sqlite_active_failures", unit="{source}")
        self.active_sources = meter.create_up_down_counter("agent_sqlite_active_sources", unit="{source}")
        self.terminal_count = meter.create_counter("agent_saved_terminals", unit="{record}")
        self.duration = meter.create_histogram("agent_saved_duration", unit="s")

    def start(self) -> None:
        """在进程外启动独立读取线程，业务执行不等待 SQLite 观测。"""
        self.active_sources.add(1)
        self.thread.start()

    def close(self) -> None:
        """停止周期读取并完成最后一次有界读取，保留终止时的来源状态。"""
        self.stop.set()
        self.thread.join()
        self.active_sources.add(-1)
        if self.failed_since is not None:
            self.active_failures.add(-1)

    def _loop(self) -> None:
        """每两秒取得绑定快照，结束时读取最终保存的记录。"""
        while not self.stop.is_set():
            self._poll()
            self.stop.wait(2.0)
        self._poll()

    def _poll(self) -> None:
        """记录真实读取错误及中断区间，恢复后已有终态用于调查。"""
        session_id = self.session()
        if session_id is None:
            return
        observed_at = time.time()
        try:
            records = read_saved_records(self.database, directory=self.directory, session_id=session_id)
            for record in records:
                self._record(record, observed_at)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.baseline = True
            self.failures.add(1, {"error_type": type(error).__name__})
            if self.failed_since is None:
                self.failed_since = observed_at
                self.active_failures.add(1)
            with trace.get_tracer("mememeow.agent").start_as_current_span("agent.sqlite.read_error", context=self.context, attributes=self.attributes) as span:
                record_content(span, "source.error", {"reason": str(error), "error_code": getattr(error, "sqlite_errorname", type(error).__name__), "missing_since": self.failed_since, "last_success": self.last_success}, secrets=self.secrets)
            logger.error("agent_sqlite_read_error: {}", protected_content(str(error), self.secrets))
            return
        if self.failed_since is not None:
            with trace.get_tracer("mememeow.agent").start_as_current_span("agent.sqlite.read_recovered", context=self.context, attributes=self.attributes) as span:
                record_content(span, "source.missing_interval", {"from": self.failed_since, "to": observed_at}, secrets=self.secrets)
            self.failed_since = None
            self.active_failures.add(-1)
        self.last_success = observed_at
        self.success_time.set(observed_at)
        self.baseline = False

    def _record(self, record: dict[str, Any], observed_at: float) -> None:
        """按来源 ID 识别更新，以消息、步骤、工具各自单位统计新的终态。"""
        identity = (record["kind"], record["id"])
        content = record["data"]
        version = (record["updated_ms"], json.dumps(content, sort_keys=True))
        if self.versions.get(identity) == version:
            return
        self.versions[identity] = version
        unit = "message" if record["kind"] == "message" else "tool" if content.get("type") == "tool" else "step" if content.get("type") == "step-finish" else "content"
        clock = content.get("time") or (content.get("state") or {}).get("time") or {}
        terminal = (record["kind"] == "message" and content.get("role") == "assistant" and (clock.get("completed") is not None or content.get("error") is not None)) or (unit == "tool" and (content.get("state") or {}).get("status") in {"completed", "error"}) or unit == "step"
        labels = {"unit": unit, "result": "failed" if content.get("error") or (content.get("state") or {}).get("status") == "error" else "succeeded"}
        if terminal and identity not in self.terminals:
            self.terminals.add(identity)
            if not self.baseline:
                self.terminal_count.add(1, labels)
                start = clock.get("created", clock.get("start"))
                end = clock.get("completed", clock.get("end"))
                if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
                    self.duration.record((end - start) / 1000, {"unit": unit})
        associated = record["created_ms"] / 1000 >= self.started_at and record["updated_ms"] / 1000 <= observed_at
        attributes = dict(self.attributes)
        if not associated:
            attributes.pop("attempt.id", None)
        attributes.update({"source": "opencode_sqlite", "observation.unit": unit, "record.id": record["id"], "session.id": record["session_id"], "source.observed_at": observed_at, "attempt.association": "confirmed" if associated else "undetermined"})
        with trace.get_tracer("mememeow.agent").start_as_current_span(f"agent.saved.{unit}", context=self.context if associated else Context(), attributes=attributes) as span:
            record_content(span, "saved.record", record, secrets=self.secrets)
            span.set_attribute("content.source", "opencode_sqlite")
            if not clock:
                span.set_attribute("content.missing_fields", "saved_time_fields")
