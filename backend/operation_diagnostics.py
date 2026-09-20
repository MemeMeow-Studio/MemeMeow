# 公共业务诊断：累计本次操作各阶段耗时，通过宿主已有的 Loguru 输出记录终态。

from __future__ import annotations

import asyncio
import json
import re
from time import perf_counter
from types import TracebackType
from typing import Any

from loguru import logger


_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")
_TRACE = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"digest:[a-f0-9]{16}\Z")
_CORRELATION_KEYS = ("task_digest", "attempt_digest", "executor_attempt_digest", "scope_digest")


def _render_event(record: dict[str, Any]) -> None:
    """把已验证的关联字段加入 JSON 消息，兼容只输出 message 的宿主日志。"""
    extra = record["extra"]
    fields = dict(extra["operation_fields"])
    trace_id = extra.get("trace_id")
    if isinstance(trace_id, str) and _TRACE.fullmatch(trace_id):
        fields["trace_id"] = trace_id
    for key in _CORRELATION_KEYS:
        value = extra.get(key)
        if isinstance(value, str) and _DIGEST.fullmatch(value):
            fields[key] = value
    record["message"] = record["message"] + " " + json.dumps(fields, ensure_ascii=False, sort_keys=True)


class OperationDiagnostics:
    """单次操作的阶段计时器；实例由调用方局部持有，业务逻辑不读取计时结果。

    phase 切换累计互不重叠的耗时，fields 只接收调用方选定的状态与计数。
    关联标识由宿主通过 logger.contextualize 提供，不修改业务接口。
    """

    def __init__(self, event: str, *, phase: str, **fields: object) -> None:
        """为固定事件名和初始阶段建立计时，fields 不得包含正文、URL 或凭据。"""
        self.event = event
        self.fields = dict(fields)
        self._started = perf_counter()
        self._phase_started = self._started
        self._phase = phase
        self._timings: dict[str, float] = {}
        self._cancelled_at: float | None = None

    def __enter__(self) -> OperationDiagnostics:
        """返回当前计时器，供一次业务操作使用。"""
        return self

    def phase(self, name: str) -> None:
        """结束当前阶段并开始指定阶段；重复阶段的耗时累加。"""
        now = perf_counter()
        self._timings[self._phase] = self._timings.get(self._phase, 0.0) + now - self._phase_started
        self._phase_started = now
        self._phase = name

    def failure(self, error: BaseException) -> None:
        """在错误被转换或保存前记录原因，仅保留错误码、异常类别和 SQLSTATE。"""
        self.fields.setdefault("error_stage", self._phase)
        self.fields.setdefault("error_type", type(error).__name__)
        code = getattr(error, "code", None)
        if isinstance(code, str) and _CODE.fullmatch(code):
            self.fields.setdefault("error_code", code)
        cause = error.__cause__
        if cause is not None:
            self.fields.setdefault("cause_type", type(cause).__name__)
            cause_code = getattr(cause, "code", None)
            if isinstance(cause_code, str) and _CODE.fullmatch(cause_code):
                self.fields.setdefault("cause_code", cause_code)
        # SQLAlchemy 将驱动异常保存在 orig；保留故障类别及约束名称，不输出 SQL 或参数。
        for candidate in (error, cause):
            original = getattr(candidate, "orig", candidate)
            sqlstate = getattr(original, "sqlstate", None)
            if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                self.fields.setdefault("sqlstate", sqlstate)
            constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
            if isinstance(constraint, str) and _CODE.fullmatch(constraint):
                self.fields.setdefault("constraint_name", constraint)

    def cancelled(self) -> None:
        """登记首次取消时间，以便记录取消后等待外部操作及保存结果的时间。"""
        if self._cancelled_at is None:
            self._cancelled_at = perf_counter()
            self.fields["cancel_stage"] = self._phase
        self.fields["cancel_requested"] = True

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        """输出一次终态汇总；异常继续传播，记录不包含异常正文。"""
        if exc is not None:
            if isinstance(exc, asyncio.CancelledError):
                self.cancelled()
                self.fields["outcome"] = "cancelled"
            else:
                self.failure(exc)
                code = getattr(exc, "code", None)
                if isinstance(code, str) and _CODE.fullmatch(code):
                    self.fields["final_error_code"] = code
                self.fields["final_error_type"] = type(exc).__name__
                self.fields["final_error_stage"] = self._phase
                self.fields["outcome"] = "unknown_execution" if code == "reverse_image_unknown_execution" else "failed"
        self.phase(self._phase)
        now = self._phase_started
        fields = {**self.fields, **{f"{name}_ms": round(seconds * 1000, 3) for name, seconds in self._timings.items()}}
        fields.setdefault("outcome", "success")
        fields["duration_ms"] = round((now - self._started) * 1000, 3)
        if self._cancelled_at is not None:
            fields["cancel_wait_ms"] = round((now - self._cancelled_at) * 1000, 3)
        level = "WARNING" if exc is not None or "error_code" in fields else "INFO"
        logger.bind(operation_fields=fields).patch(_render_event).log(level, self.event)
