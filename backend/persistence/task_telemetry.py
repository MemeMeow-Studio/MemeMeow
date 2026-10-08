# 持久 Task 观测：从已有 Session 的状态变化生成提交后的终态指标。
from __future__ import annotations

import re

from opentelemetry import metrics
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, sessionmaker

from backend.persistence.models import Task

_PENDING = "task_terminal_telemetry"
_ENABLED = "task_terminal_telemetry_enabled"
_TERMINALS = {"succeeded", "failed", "cancelled"}


def terminal_snapshot(task_type: str, status: str, error: object) -> dict[str, str] | None:
    """将已有业务字段转成有界指标标签，非终态返回空值。"""
    if status not in _TERMINALS:
        return None
    reason = error.get("error") or error.get("code") if isinstance(error, dict) else None
    if not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason) is None:
        reason = "none" if status == "succeeded" else "unspecified"
    result = "cancelled" if status == "failed" and reason == "task_cancelled" else status
    return {"task_type": task_type, "result": result, "reason": reason}


def stage_terminal(session: Session, identifier: object, snapshot: dict[str, str] | None) -> None:
    """在当前业务事务内暂存内存快照，SQL 更新路径复用同一个入口。"""
    if not session.info.get(_ENABLED):
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("task_telemetry_transaction_missing")
    session.info.setdefault(_PENDING, {}).setdefault(transaction, {})[identifier] = snapshot


def _before_flush(session: Session, flush_context: object, instances: object) -> None:
    """观察 ORM 已修改的 Task，不执行查询或创建 Session。"""
    for record in session.new | session.dirty:
        if isinstance(record, Task) and (record in session.new or inspect(record).attrs.status.history.has_changes()):
            stage_terminal(session, record.id, terminal_snapshot(record.task_type, record.status, record.error))


def _after_commit(session: Session) -> None:
    """保存点合入父事务，最外层提交成功后增加真实终态计数。"""
    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.get(_PENDING, {})
    snapshots = pending.pop(transaction, {})
    if transaction is not None and transaction.parent is not None:
        pending.setdefault(transaction.parent, {}).update(snapshots)
        return
    counter = metrics.get_meter("mememeow.tasks").create_counter("task_terminals", unit="{task}")
    for snapshot in snapshots.values():
        if snapshot is not None:
            counter.add(1, snapshot)


def _after_transaction_end(session: Session, transaction: object) -> None:
    """事务结束后移除未提交快照，回滚不会产生成功指标。"""
    pending = session.info.get(_PENDING, {})
    pending.pop(transaction, None)
    if not pending:
        session.info.pop(_PENDING, None)


def instrument_task_sessions(factory: sessionmaker[Session]) -> None:
    """仅为所属资源工厂注册标准 Session 事件，覆盖取消、恢复及 Worker 终态。"""
    factory.configure(info={**factory.kw.get("info", {}), _ENABLED: True})
    event.listen(factory, "before_flush", _before_flush)
    event.listen(factory, "after_commit", _after_commit)
    event.listen(factory, "after_transaction_end", _after_transaction_end)
