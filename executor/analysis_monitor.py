# Executor 进程循环中的分析用量控制，负责就绪检查及终止原因判定。

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import time

from executor.analysis_usage import AnalysisUsageError, read_analysis_usage

PLUGIN_VERSION = "1.18.18"


class AnalysisControlError(RuntimeError):
    """携带可公开错误码和受保护原因，交给现有 supervisor 回收进程。"""

    def __init__(self, code: str, reason: str):
        """保存控制失败类别和阶段，不包含模型正文。"""
        self.code, self.reason = code, reason
        super().__init__(reason)


@dataclass
class AnalysisMonitor:
    """当前 attempt 的检查状态；恢复输入保留原策略和最近可信金额。"""

    attempt_id: str
    policy: dict[str, object]
    database: Path
    directory: Path
    status_path: Path
    startup_deadline: float
    observed_cost: Decimal = Decimal(0)
    usage_checked_at: str | None = None
    reminder_sent: bool = False
    ready: bool = False
    next_check: float = 0
    reminder_error: str | None = None

    def check(self, session_id: str | None, *, exited: bool = False) -> None:
        """轮询当前 attempt 的就绪及金额；终止原因通过异常交给进程管理者。"""
        now = time.monotonic()
        if not exited and now < self.next_check:
            return
        self.next_check = now + 0.25
        if self.status_path.exists():
            try:
                status = json.loads(self.status_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise AnalysisControlError("agent_analysis_reminder_plugin_unavailable", "plugin_status_unreadable") from exc
            if (
                not isinstance(status, dict)
                or status.get("attempt_id") != self.attempt_id
                or status.get("plugin_version") != PLUGIN_VERSION
                or status.get("policy_version") != self.policy["version"]
            ):
                raise AnalysisControlError("agent_analysis_reminder_plugin_unavailable", "plugin_attempt_binding_mismatch")
            self.ready = status.get("ready") is True
            self.reminder_sent = self.reminder_sent or status.get("reminder_sent") is True
            self.reminder_error = status.get("error") if isinstance(status.get("error"), str) else None
            if not self.ready and self.reminder_error:
                if self.reminder_error not in {
                    "analysis_plugin_runtime_version_incompatible",
                    "analysis_plugin_initialization_failed",
                }:
                    raise AnalysisControlError("agent_analysis_reminder_plugin_unavailable", "plugin_error_status_invalid")
                raise AnalysisControlError("agent_analysis_reminder_plugin_unavailable", self.reminder_error)
            if session_id and status.get("session_id") not in (None, session_id):
                raise AnalysisControlError("agent_analysis_usage_unavailable", "plugin_session_binding_mismatch")
        if session_id:
            try:
                usage = read_analysis_usage(
                    self.database, session_id=session_id, directory=self.directory,
                    minimum_cost=self.observed_cost,
                )
            except AnalysisUsageError as exc:
                raise AnalysisControlError(exc.code, exc.reason) from exc
            self.observed_cost = usage.observed_cost
            self.usage_checked_at = usage.checked_at
            if self.observed_cost >= Decimal(str(self.policy["termination_cost"])):
                raise AnalysisControlError("agent_maximum_analysis_depth_exceeded", "termination_cost_reached")
        elif exited or now >= self.startup_deadline:
            raise AnalysisControlError("agent_analysis_usage_unavailable", "session_binding_deadline_exceeded")
        if not self.ready and (exited or now >= self.startup_deadline):
            reason = "process_exited_before_plugin_ready" if exited else "plugin_readiness_deadline_exceeded"
            raise AnalysisControlError("agent_analysis_reminder_plugin_unavailable", reason)
