"""反向图片 usage 事件持久化 Repository。

该模块位于任务持久化域，负责 scope、claim binding、幂等终态和任务级审计摘要；
provider、图片检索和 HTTP callback 由其它模块负责。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.persistence.engine import DatabaseError
from backend.persistence.models import ReverseImageUsageEvent, ScopeContext, utcnow


class ReverseImageUsageRepository:
    """按 scope 读写反向图片用量事件并生成任务级审计摘要。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, request_id: str, *, for_update: bool = False) -> ReverseImageUsageEvent | None:
        """幂等读取当前 scope 的请求记录；全局 request_id 仍不会跨 scope 产生统计。"""
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_binding(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> ReverseImageUsageEvent | None:
        """按完整 callback 绑定读取旧 usage 事实，拒绝不确定的历史别名。

        callback 迁移只为 callback 表安装逻辑唯一约束；旧 usage 可能在进程崩溃窗口
        中先于 callback 行落盘。若完整绑定命中多条 usage，调用方不能猜测权威 ID，
        因此以稳定冲突错误停止恢复。
        """
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.task_id == task_id,
            ReverseImageUsageEvent.claim_generation == claim_generation,
            ReverseImageUsageEvent.attempt == attempt,
            ReverseImageUsageEvent.operation == operation,
            ReverseImageUsageEvent.target_sha256 == target_sha256,
            ReverseImageUsageEvent.input_digest == input_digest,
        ).order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
        if for_update:
            statement = statement.with_for_update()
        try:
            rows = list(self.session.scalars(statement))
        except SQLAlchemyError as exc:
            raise DatabaseError("usage_binding_unavailable") from exc
        if len(rows) > 1:
            raise DatabaseError("usage_request_conflict")
        return rows[0] if rows else None

    @staticmethod
    def _same_request(
        event: ReverseImageUsageEvent,
        *,
        task_id: str,
        cache_key: str,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """校验 request id 的完整执行绑定，防止旧 claim 借新输入复用事实。"""
        if event.task_id != task_id or event.cache_key != cache_key:
            raise DatabaseError("usage_request_conflict")
        expected = (claim_generation, attempt, operation, target_sha256, input_digest)
        actual = (event.claim_generation, event.attempt, event.operation, event.target_sha256, event.input_digest)
        # 旧事件的全空绑定仍可由旧兼容入口读取；一旦事件或本次请求带有
        # 执行绑定，就必须逐字段一致，不能用缺省字段绕过 request id 约束。
        if (any(value is not None for value in expected) or any(value is not None for value in actual)) and actual != expected:
            raise DatabaseError("usage_request_conflict")
        return event

    def create(
        self,
        *,
        request_id: str,
        task_id: str,
        meme_id: UUID | str | None,
        cache_key: str,
        cache_status: str,
        provider: str | None = None,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """创建一次逻辑检索事件；重复 request_id 返回现有事件以保持幂等。"""
        parsed_meme_id: UUID | None = None
        if meme_id:
            try:
                parsed_meme_id = UUID(str(meme_id))
            except (ValueError, TypeError) as exc:
                raise DatabaseError("meme_not_found") from exc
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        event = ReverseImageUsageEvent(
            request_id=request_id,
            scope_id=self.scope.scope_id,
            task_id=task_id,
            meme_id=parsed_meme_id,
            cache_key=cache_key,
            cache_status=cache_status,
            provider=provider,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            # 保存点只回滚这次幂等插入，不能撤销调用方 UOW 中已经完成的任务写入。
            with self.session.begin_nested():
                self.session.add(event)
                self.session.flush()
        except IntegrityError:
            existing = self.get(request_id, for_update=True)
            if existing is None:
                raise DatabaseError("usage_event_conflict")
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        return event

    def mark_provider_started(self, request_id: str) -> ReverseImageUsageEvent:
        """原子标记供应商逻辑调用已开始，计数只会从 false 变为 true 一次。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        if event.completed_at is not None:
            return event
        if not event.provider_called:
            event.provider_called = True
            event.provider_started_at = utcnow()
            event.outcome = "started"
            self.session.flush()
        return event

    def finish(
        self,
        request_id: str,
        *,
        cache_status: str | None = None,
        outcome: str,
        retryable: bool = False,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ReverseImageUsageEvent:
        """写入供应商或缓存终态；失败事件保留以供恢复和审计。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        # 已完成事件是幂等终态；重试不能用另一份响应覆盖原始审计事实。
        if event.completed_at is not None:
            return event
        if cache_status is not None:
            event.cache_status = cache_status
        event.outcome = outcome
        event.retryable = retryable
        event.result = result
        event.error = error
        event.completed_at = utcnow()
        self.session.flush()
        return event

    def aggregate_task(self, task_id: str) -> dict[str, Any]:
        """按当前 scope 聚合任务事件，返回不含图片身份和供应商秘密的摘要。"""
        rows = list(
            self.session.scalars(
                select(ReverseImageUsageEvent)
                .where(
                    ReverseImageUsageEvent.scope_id == self.scope.scope_id,
                    ReverseImageUsageEvent.task_id == task_id,
                )
                .order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
            )
        )
        completed = sorted(
            (row for row in rows if row.completed_at is not None),
            key=lambda row: (row.completed_at, row.created_at, row.id),
        )
        return {
            "attempted": bool(rows),
            "used": any(row.outcome in {"success", "empty"} and bool((row.result or {}).get("used", True)) for row in rows),
            "cache_hits": sum(1 for row in rows if row.cache_status == "hit"),
            "provider_calls": sum(1 for row in rows if row.provider_called),
            "outcome": completed[-1].outcome if completed else ("started" if rows else "not_requested"),
            "request_count": len(rows),
        }


__all__ = ["ReverseImageUsageRepository"]
