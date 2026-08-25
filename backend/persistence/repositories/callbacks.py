"""Agent callback request 事实持久化 Repository。

该模块位于任务持久化域，负责完整执行绑定、逻辑唯一性、幂等终态和内存测试夹具；
token 签发/验证、HTTP 路由和外部 provider 不属于本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.persistence.engine import DatabaseError, SCOPE_LOCAL
from backend.persistence.models import AgentCallbackRequest, ScopeContext, utcnow


def _validate_callback_binding_values(
    *,
    request_id: str | None,
    task_id: str,
    claim_generation: int,
    attempt: int,
    operation: str,
    target_sha256: str,
    input_digest: str,
) -> None:
    """在事实层拒绝缺失或不完整的 callback 绑定字段。

    API 层已经验证过 callback 输入，但内存夹具、迁移工具和其它宿主适配器也可能
    直接调用 repository；这些路径不能因为绕过 HTTP 校验而退回 request-ID-only
    语义。数据库约束负责并发唯一性，这里负责在进入索引前保持两种事实层一致。
    """
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for char in request_id)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > 255
        or isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or not isinstance(operation, str)
        or not operation
        or len(operation) > 128
        or not isinstance(target_sha256, str)
        or len(target_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in target_sha256)
        or not isinstance(input_digest, str)
        or len(input_digest) != 64
        or any(char not in "0123456789abcdef" for char in input_digest)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")


class AgentCallbackRequestRepository:
    """按 scope 保存 callback request 绑定，供只读和副作用 callback 复用。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def ensure_schema_ready(self) -> bool:
        """确认 callback 复合唯一索引和绑定字段已经安装。

        callback 是可选的内部路径，不能因为旧安装尚未迁移而阻断 local 直连；但一旦
        请求进入该 repository，缺失索引、可空事实或表结构异常必须 fail-closed。
        """
        try:
            bind = self.session.get_bind()
            if bind is None or bind.dialect.name != "postgresql":
                raise DatabaseError("callback_binding_schema_unavailable")
            required_columns = (
                "scope_id",
                "task_id",
                "claim_generation",
                "attempt",
                "operation",
                "target_sha256",
                "input_digest",
            )
            columns_sql = ", ".join(f"'{column}'" for column in required_columns)
            row = self.session.execute(
                text(
                    f"""
                    SELECT
                        EXISTS(
                            SELECT 1
                              FROM pg_indexes
                             WHERE schemaname = current_schema()
                               AND tablename = 'agent_callback_requests'
                               AND indexname = 'uq_agent_callback_requests_logical'
                        ),
                        (
                            SELECT count(*)
                              FROM information_schema.columns
                             WHERE table_schema = current_schema()
                               AND table_name = 'agent_callback_requests'
                               AND column_name IN ({columns_sql})
                               AND is_nullable = 'NO'
                        )
                    """
                )
            ).one()
        except (SQLAlchemyError, DatabaseError) as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError("callback_binding_schema_unavailable") from exc
        if not bool(row[0]) or int(row[1]) != len(required_columns):
            raise DatabaseError("callback_binding_schema_unavailable")
        return True

    # 启动检查和隐藏测试使用的显式命名，均保持同一事实来源。
    is_schema_ready = ensure_schema_ready

    def get(self, request_id: str, *, for_update: bool = False) -> AgentCallbackRequest | None:
        """读取当前 scope 的 request 事实，不让 request id 跨 scope 产生命中。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_logical(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> AgentCallbackRequest | None:
        """按完整执行绑定和规范化输入读取唯一权威 callback 事实。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.task_id == task_id,
            AgentCallbackRequest.claim_generation == claim_generation,
            AgentCallbackRequest.attempt == attempt,
            AgentCallbackRequest.operation == operation,
            AgentCallbackRequest.target_sha256 == target_sha256,
            AgentCallbackRequest.input_digest == input_digest,
        )
        if for_update:
            statement = statement.with_for_update()
        try:
            return self.session.scalar(statement)
        except SQLAlchemyError as exc:
            # 历史重复逻辑键在迁移前不能猜测哪一行权威，读取也必须停止。
            raise DatabaseError("callback_binding_schema_unavailable") from exc

    @staticmethod
    def _same(
        row: AgentCallbackRequest,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """比较 callback request 的完整可信绑定，拒绝改绑重放。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def create(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """创建或复用 callback 逻辑事实，request ID 仅作为兼容性提示。"""
        return self.resolve(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )

    def resolve(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """按 request ID 和完整逻辑键解析唯一权威 callback 事实。

        解析顺序先比较当前 scope 下的 request ID，再查逻辑唯一键，最后才插入。并发
        插入遇到复合唯一约束冲突时重新读取竞争者事实，绝不通过新 ID 生成第三行。
        """
        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        self.ensure_schema_ready()
        existing = self.get(request_id, for_update=True) if request_id is not None else None
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        authoritative = self.get_by_logical(
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
            for_update=True,
        )
        if authoritative is not None:
            return authoritative
        if request_id is None:
            from backend.callbacks import canonical_callback_request_id

            request_id = canonical_callback_request_id(input_digest)
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        row = AgentCallbackRequest(
            scope_id=self.scope.scope_id,
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            authoritative = self.get_by_logical(
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
                for_update=True,
            )
            if authoritative is not None:
                return authoritative
            existing = self.get(request_id, for_update=True)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            raise DatabaseError("callback_request_conflict") from exc
        return row

    def finish(
        self,
        request_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> AgentCallbackRequest:
        """以幂等终态保存 callback 结果，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        row = self.get(request_id, for_update=True)
        if row is None:
            raise DatabaseError("callback_request_not_found")
        if row.completed_at is not None:
            # ``unknown_execution`` 只表示外部调用当时无法确认；随后若持久 usage
            # 事实已经明确成功或失败，可以收束为对应终态，但普通重试不会重新触发
            # provider，因为调用方会先读取该终态。
            if row.state != "unknown_execution" or state not in {"completed", "failed"}:
                return row
        row.state = state
        row.result = result
        row.error = error
        row.completed_at = utcnow()
        row.updated_at = row.completed_at
        self.session.flush()
        return row


@dataclass(slots=True)
class InMemoryCallbackRequest:
    """单进程 callback 事实夹具，字段与 PostgreSQL 模型保持一致。"""

    scope_id: str
    request_id: str
    task_id: str
    claim_generation: int
    attempt: int
    operation: str
    target_sha256: str
    input_digest: str
    state: str = "started"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = None  # type: ignore[assignment]
    updated_at: datetime = None  # type: ignore[assignment]
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """为内存夹具补齐与 ORM 默认值等价的创建时间。"""
        now = utcnow()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at


class InMemoryAgentCallbackRequestRepository:
    """提供与 PostgreSQL 相同双索引语义的单进程 callback repository。

    该夹具用于快速单元测试；生产并发门禁仍由 PostgreSQL 复合唯一索引提供。锁内
    同时维护 request ID 和完整逻辑键，确保不同 ID 的首次竞态只能得到同一事实。
    """

    def __init__(self, scope: ScopeContext | str = SCOPE_LOCAL):
        self.scope = scope if isinstance(scope, ScopeContext) else ScopeContext(scope)
        self._by_id: dict[str, InMemoryCallbackRequest] = {}
        self._by_logical: dict[tuple[str, int, int, str, str, str], InMemoryCallbackRequest] = {}
        self._lock = RLock()

    @staticmethod
    def _logical_key(*, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> tuple[str, int, int, str, str, str]:
        """构造与数据库唯一约束字段顺序一致的内存索引键。"""
        return (task_id, claim_generation, attempt, operation, target_sha256, input_digest)

    @staticmethod
    def _same(row: InMemoryCallbackRequest, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """比较完整绑定，拒绝 request ID 改绑。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def get(self, request_id: str) -> InMemoryCallbackRequest | None:
        """读取当前 scope 的 request ID 事实。"""
        with self._lock:
            return self._by_id.get(request_id)

    def get_by_logical(self, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest | None:
        """按完整逻辑键读取权威事实。"""
        with self._lock:
            return self._by_logical.get(self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest))

    def resolve(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """按 ID、逻辑键和确定性 ID 的顺序解析或创建 callback 事实。"""
        from backend.callbacks import canonical_callback_request_id

        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        with self._lock:
            existing = self._by_id.get(request_id) if request_id is not None else None
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            key = self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            authoritative = self._by_logical.get(key)
            if authoritative is not None:
                return authoritative
            selected_id = request_id or canonical_callback_request_id(input_digest)
            existing = self._by_id.get(selected_id)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            row = InMemoryCallbackRequest(
                scope_id=self.scope.scope_id,
                request_id=selected_id,
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
            self._by_id[selected_id] = row
            self._by_logical[key] = row
            return row

    def create(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """兼容旧调用名，执行双索引 resolve。"""
        return self.resolve(request_id=request_id, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)

    def finish(self, request_id: str, *, state: str, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> InMemoryCallbackRequest:
        """幂等保存 callback 终态，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        with self._lock:
            row = self._by_id.get(request_id)
            if row is None:
                raise DatabaseError("callback_request_not_found")
            if row.completed_at is not None and (row.state != "unknown_execution" or state not in {"completed", "failed"}):
                return row
            row.state = state
            row.result = result
            row.error = error
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            return row


# 兼容不同测试和宿主命名，避免为 callback 事实引入第二套 repository。
InMemoryCallbackRequestRepository = InMemoryAgentCallbackRequestRepository


__all__ = [
    "AgentCallbackRequestRepository",
    "InMemoryCallbackRequest",
    "InMemoryAgentCallbackRequestRepository",
    "InMemoryCallbackRequestRepository",
]
