# Executor 的分析用量读取：只查询已绑定主 session 的 OpenCode 累计金额。

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


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
    cost = Decimal(str(value))
    if cost < minimum_cost:
        raise AnalysisUsageError("session_cost_decreased")
    return AnalysisUsage(cost, datetime.now(timezone.utc).isoformat())
