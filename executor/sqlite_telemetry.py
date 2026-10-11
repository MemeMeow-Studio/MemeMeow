# Executor 的 OpenCode 只读观测，读取范围来自当前任务的可信执行绑定。
from __future__ import annotations

import json
import math
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

from runtime_telemetry import emit_event, protected_content, record_content


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
        session_rows = connection.execute(f"SELECT id, time_created, time_updated, parent_id, title, version, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write FROM session WHERE id IN ({placeholders}) ORDER BY time_updated, id", identifiers)
        records = [{"kind": "session", "id": row[0], "session_id": row[0], "created_ms": row[1], "updated_ms": row[2], "data": dict(zip(("parent_id", "title", "version", "cost", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write"), row[3:]))} for row in session_rows]
        for table in ("message", "part"):
            message_column = "message_id" if table == "part" else "id"
            rows = connection.execute(f"SELECT id, session_id, time_created, time_updated, data, {message_column} FROM {table} WHERE session_id IN ({placeholders}) ORDER BY time_updated, id", identifiers)
            for identifier, bound_session, created, updated, data, message_id in rows:
                content = SavedContent.model_validate_json(data).model_dump(exclude_none=True)
                records.append({"kind": table, "id": identifier, "message_id": message_id, "session_id": bound_session, "created_ms": created, "updated_ms": updated, "data": content})
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
        self.usage_values: dict[str, dict[str, float]] = {}
        self.usage_baselines: set[str] = set()
        self.message_models: dict[str, dict[str, str]] = {}
        meter = metrics.get_meter("mememeow.agent")
        self.success_time = meter.create_gauge("agent_sqlite_last_success", unit="s")
        self.failures = meter.create_counter("agent_sqlite_read_errors", unit="{error}")
        self.active_failures = meter.create_up_down_counter("agent_sqlite_active_failures", unit="{source}")
        self.active_sources = meter.create_up_down_counter("agent_sqlite_active_sources", unit="{source}")
        self.terminal_count = meter.create_counter("agent_saved_terminals", unit="{record}")
        self.duration = meter.create_histogram("agent_saved_duration", unit="s")
        self.tokens = meter.create_counter("agent_saved_tokens", unit="{token}")
        self.cost = meter.create_counter("agent_saved_cost", unit="USD")
        self.usage_issues = meter.create_counter("agent_usage_issues", unit="{field}")
        self.tool_issues = meter.create_counter("agent_tool_record_issues", unit="{record}")
        self.baseline_time = meter.create_gauge("agent_usage_baseline_time", unit="s")

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
        """每六秒取得绑定快照，结束时读取最终保存的记录。"""
        while not self.stop.is_set():
            self._poll()
            self.stop.wait(6.0)
        self._poll(final=True)

    def _poll(self, *, final: bool = False) -> None:
        """记录真实读取错误及中断区间，恢复后已有终态用于调查。"""
        session_id = self.session()
        observed_at = time.time()
        try:
            if session_id is None:
                if final:
                    raise ValueError("sqlite_session_binding_missing_at_attempt_end")
                return
            records = read_saved_records(self.database, directory=self.directory, session_id=session_id)
            self.message_models = {record["id"]: {label: record["data"][field] for label, field in (("provider", "providerID"), ("model", "modelID")) if isinstance(record["data"].get(field), str)} for record in records if record["kind"] == "message"}
            if self.baseline:
                self.baseline_time.set(observed_at)
                emit_event("sqlite_diagnostics", {"event": "usage_baseline", "observed_at": observed_at, "started_at": self.started_at, "measurement_scope": "已有完成步骤建立基线；后续完成步骤及有效正向修订计数", **self.attributes})
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
            emit_event("sqlite_diagnostics", {"event": "read_error", "reason": str(error), "error_code": getattr(error, "sqlite_errorname", type(error).__name__), "missing_since": self.failed_since, "last_success": self.last_success, **self.attributes})
            return
        if self.failed_since is not None:
            with trace.get_tracer("mememeow.agent").start_as_current_span("agent.sqlite.read_recovered", context=self.context, attributes=self.attributes) as span:
                record_content(span, "source.missing_interval", {"from": self.failed_since, "to": observed_at}, secrets=self.secrets)
            emit_event("sqlite_diagnostics", {"event": "read_recovered", "missing_from": self.failed_since, "missing_to": observed_at, **self.attributes})
            self.failed_since = None
            self.active_failures.add(-1)
        self.last_success = observed_at
        self.success_time.set(observed_at)
        self.baseline = False

    def _usage_issue(self, record: dict[str, Any], field: str, reason: str) -> None:
        """用有限类别记录字段覆盖问题，来源标识只进入受控诊断。"""
        self.usage_issues.add(1, {"field": field, "reason": reason})
        emit_event("sqlite_diagnostics", {"event": "usage_issue", "field": field, "reason": reason, "record_id": record["id"], "session_id": record["session_id"], **self.attributes})

    def _record_usage(self, record: dict[str, Any]) -> None:
        """完成步骤提供唯一用量来源，按字段累计有效新增量并保存读取基线。"""
        content = record["data"]
        tokens = content.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        values = {"input": tokens.get("input"), "output": tokens.get("output"), "reasoning": tokens.get("reasoning"), "cache_read": cache.get("read"), "cache_write": cache.get("write"), "cost": content.get("cost")}
        labels = {"provider": "unknown", "model": "unknown", **self.message_models.get(record.get("message_id"), {})}
        if "unknown" in labels.values():
            self._usage_issue(record, "association", "message_model_missing")
        previous = self.usage_values.setdefault(record["id"], {})
        establish_baseline = self.baseline and (self.last_success > 0 or self.failed_since is not None or record["created_ms"] / 1000 < self.started_at)
        if establish_baseline:
            self.usage_baselines.add(record["id"])
        for field, value in values.items():
            if value is None:
                self._usage_issue(record, field, "missing")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (field != "cost" and value != int(value)):
                self._usage_issue(record, field, "invalid")
                continue
            before = previous.get(field, 0)
            if value < before:
                self._usage_issue(record, field, "decreased")
                continue
            missing_baseline = record["id"] in self.usage_baselines and field not in previous
            previous[field] = value
            if establish_baseline or missing_baseline:
                if missing_baseline and not establish_baseline:
                    self._usage_issue(record, field, "baseline_field_became_valid")
                continue
            delta = value - before
            if field == "cost":
                self.cost.add(delta, labels)
            else:
                self.tokens.add(delta, {**labels, "token_type": field})

    def _record(self, record: dict[str, Any], observed_at: float) -> None:
        """按来源 ID 统计新的终态；工具按保存名称分组，缺失名称进入诊断。"""
        identity = (record["kind"], record["id"])
        content = record["data"]
        version = (record["updated_ms"], json.dumps(content, sort_keys=True))
        if self.versions.get(identity) == version and not (self.baseline and content.get("type") == "step-finish"):
            return
        self.versions[identity] = version
        if record["kind"] == "part" and content.get("type") == "step-finish":
            self._record_usage(record)
        unit = "session" if record["kind"] == "session" else "message" if record["kind"] == "message" else "tool" if content.get("type") == "tool" else "step" if content.get("type") == "step-finish" else "content"
        clock = content.get("time") or (content.get("state") or {}).get("time") or {}
        terminal = (record["kind"] == "message" and content.get("role") == "assistant" and (clock.get("completed") is not None or content.get("error") is not None)) or (unit == "tool" and (content.get("state") or {}).get("status") in {"completed", "error"}) or unit == "step"
        labels = {"unit": unit, "result": "failed" if content.get("error") or (content.get("state") or {}).get("status") == "error" else "succeeded"}
        labels.update({label: content[field] for label, field in (("provider", "providerID"), ("model", "modelID")) if isinstance(content.get(field), str)})
        tool = content.get("tool") if unit == "tool" else None
        if unit == "tool":
            labels.update(self.message_models.get(record.get("message_id"), {}))
            labels["result"] = "failed" if (content.get("state") or {}).get("status") == "error" else "succeeded"
            if isinstance(tool, str) and tool.strip():
                labels["tool"] = tool
                for result in ("succeeded", "failed"):
                    self.terminal_count.add(0, {**labels, "result": result})
            else:
                reason = "missing" if tool is None else "invalid"
                self.tool_issues.add(1, {"field": "tool", "reason": reason})
                emit_event("sqlite_diagnostics", {"event": "tool_record_issue", "field": "tool", "reason": reason, "record_id": record["id"], "session_id": record["session_id"], **self.attributes})
        if terminal and identity not in self.terminals:
            self.terminals.add(identity)
            if not self.baseline or (self.last_success == 0 and self.failed_since is None and record["created_ms"] / 1000 >= self.started_at):
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
        if unit == "tool":
            if "tool" in labels:
                attributes["tool.name"] = labels["tool"]
            status = (content.get("state") or {}).get("status")
            if isinstance(status, str):
                attributes["tool.status"] = status
        with trace.get_tracer("mememeow.agent").start_as_current_span(f"agent.saved.{unit}", context=self.context if associated else Context(), attributes=attributes) as span:
            record_content(span, "saved.record", record, secrets=self.secrets)
            span.set_attribute("content.source", "opencode_sqlite")
            if not clock:
                span.set_attribute("content.missing_fields", "saved_time_fields")
            elif terminal and not (isinstance(clock.get("created", clock.get("start")), (int, float)) and isinstance(clock.get("completed", clock.get("end")), (int, float))):
                span.set_attribute("content.missing_fields", "saved_duration_fields")
