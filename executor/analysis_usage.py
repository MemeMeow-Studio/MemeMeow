# Executor 的分析用量读取：生产查询 broker attempt 金额，本地兼容查询绑定 session。

from __future__ import annotations

import json
import math
import socket
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from executor.model_capability import validate_model_broker_url, validate_model_capability


ANALYSIS_USAGE_RESPONSE_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝 broker 用量查询跳转，防止 capability 被转发到其它地址。"""

    def redirect_request(self, *_args: Any, **_kwargs: Any):
        """拒绝所有 HTTP 跳转。"""
        return None


_BROKER_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)


class AnalysisUsageError(RuntimeError):
    """保留稳定错误码及可定位读取阶段的原因。"""

    code = "agent_analysis_usage_unavailable"

    def __init__(self, reason: str):
        """接收不含业务数据的原因，用于 Executor 终止诊断。"""
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AnalysisUsage:
    """一次主 session 只读快照，包含美元累计值及 UTC 检查时间。"""

    observed_cost: Decimal
    checked_at: str


def _validated_usage(*, observed_cost: object, checked_at: object, minimum_cost: Decimal) -> AnalysisUsage:
    """校验公共金额快照，拒绝无效金额、无时区时间和金额倒退。"""
    if not isinstance(observed_cost, (str, int, float)) or isinstance(observed_cost, bool):
        raise AnalysisUsageError("session_cost_invalid")
    try:
        cost = Decimal(str(observed_cost))
    except (InvalidOperation, ValueError) as exc:
        raise AnalysisUsageError("session_cost_invalid") from exc
    if not cost.is_finite() or cost < 0:
        raise AnalysisUsageError("session_cost_invalid")
    if cost < minimum_cost:
        raise AnalysisUsageError("session_cost_decreased")
    if not isinstance(checked_at, str):
        raise AnalysisUsageError("usage_checked_at_invalid")
    try:
        parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisUsageError("usage_checked_at_invalid") from exc
    if parsed.tzinfo is None:
        raise AnalysisUsageError("usage_checked_at_invalid")
    return AnalysisUsage(cost, parsed.astimezone(timezone.utc).isoformat())


def read_broker_analysis_usage(
    broker_url: str,
    *,
    capability: str,
    attempt_id: str,
    minimum_cost: Decimal = Decimal(0),
    timeout_seconds: float = 2,
) -> AnalysisUsage:
    """读取模型 broker 保存的 attempt 累计金额，生产终止控制以此为权威。"""
    endpoint = f"{validate_model_broker_url(broker_url)}/analysis-usage"
    token = validate_model_capability(capability)
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 255:
        raise AnalysisUsageError("attempt_binding_invalid")
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-MemeMeow-Executor-Attempt-ID": attempt_id,
        },
        method="GET",
    )
    try:
        with _BROKER_OPENER.open(request, timeout=timeout_seconds) as response:
            raw = response.read(ANALYSIS_USAGE_RESPONSE_BYTES + 1)
            status = int(getattr(response, "status", response.getcode()))
    except urllib.error.HTTPError as exc:
        raise AnalysisUsageError(f"broker_usage_http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = "broker_usage_timeout" if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)) else "broker_usage_unavailable"
        raise AnalysisUsageError(reason) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise AnalysisUsageError("broker_usage_timeout") from exc
    except OSError as exc:
        raise AnalysisUsageError("broker_usage_unavailable") from exc
    if status != 200:
        raise AnalysisUsageError(f"broker_usage_http_{status}")
    if len(raw) > ANALYSIS_USAGE_RESPONSE_BYTES:
        raise AnalysisUsageError("broker_usage_response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisUsageError("broker_usage_response_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"executor_attempt_id", "observed_cost", "checked_at"}:
        raise AnalysisUsageError("broker_usage_response_invalid")
    if payload.get("executor_attempt_id") != attempt_id:
        raise AnalysisUsageError("attempt_binding_mismatch")
    return _validated_usage(
        observed_cost=payload.get("observed_cost"),
        checked_at=payload.get("checked_at"),
        minimum_cost=minimum_cost,
    )


def read_analysis_usage(
    database: Path, *, session_id: str, directory: Path,
    minimum_cost: Decimal = Decimal(0),
) -> AnalysisUsage:
    """从任务 OPENCODE_DB 读取已绑定主 session；缺失、冲突及金额减少立即报错。"""
    if not session_id:
        raise AnalysisUsageError("session_binding_missing")
    if not database.is_file():
        raise AnalysisUsageError("database_missing")
    try:
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as connection:
            connection.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(session)")}
            if not {"id", "parent_id", "directory", "cost"}.issubset(columns):
                raise AnalysisUsageError("session_schema_incompatible")
            row = connection.execute(
                "SELECT parent_id, directory, cost FROM session WHERE id = ?", (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        reason = getattr(exc, "sqlite_errorname", type(exc).__name__)
        raise AnalysisUsageError(f"database_read_error:{reason}") from exc
    if row is None:
        raise AnalysisUsageError("session_missing")
    if row[0] is not None or row[1] != str(directory):
        raise AnalysisUsageError("session_binding_mismatch")
    value = row[2]
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise AnalysisUsageError("session_cost_invalid")
    return _validated_usage(
        observed_cost=value,
        checked_at=datetime.now(timezone.utc).isoformat(),
        minimum_cost=minimum_cost,
    )
