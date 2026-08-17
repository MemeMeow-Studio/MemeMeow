"""与用户账户无关的 operation policy 和 grant 生命周期。

该模块位于图片上传、任务 Worker 与外部 provider 的真实副作用边界。核心只传递
已验证的 ``ScopeContext``、稳定 operation 名称和服务端幂等键，不解析用户、套餐或
周期额度；适配宿主可注入自己的 policy 实现，开源入口使用显式 allow-all 实现。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, Mapping, Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.database import OperationGrant, ScopeContext, Task, utcnow


class Operations:
    """首期公共 operation vocabulary。"""

    IMAGE_UPLOAD = "image.upload"
    ANALYSIS_AGENT = "analysis.agent"
    ANALYSIS_REVERSE_IMAGE_SEARCH = "analysis.reverse_image_search"
    IMAGE_DELETE = "image.delete"
    ALL = frozenset({IMAGE_UPLOAD, ANALYSIS_AGENT, ANALYSIS_REVERSE_IMAGE_SEARCH, IMAGE_DELETE})


OPERATION_IMAGE_UPLOAD = Operations.IMAGE_UPLOAD
OPERATION_ANALYSIS_AGENT = Operations.ANALYSIS_AGENT
OPERATION_ANALYSIS_REVERSE_IMAGE_SEARCH = Operations.ANALYSIS_REVERSE_IMAGE_SEARCH
OPERATION_IMAGE_DELETE = Operations.IMAGE_DELETE


class OperationPolicyError(RuntimeError):
    """策略边界稳定错误，不携带 policy 原始正文或商业字段。"""

    _messages = {
        "operation_forbidden": "当前操作未被允许",
        "operation_limit_exceeded": "当前操作暂不可用",
        "operation_policy_unavailable": "操作策略暂不可用",
        "operation_unknown": "操作类型无效",
        "operation_grant_invalid": "操作授权无效",
    }

    def __init__(self, code: str, *, retry_at: datetime | str | None = None) -> None:
        self.code = code if code in self._messages else "operation_policy_unavailable"
        self.retry_at = retry_at
        super().__init__(self._messages[self.code])

    def payload(self) -> dict[str, object]:
        """返回可直接映射到 HTTP 的受限错误载荷。"""
        value: dict[str, object] = {"error": self.code, "message": self._messages[self.code]}
        if self.retry_at is not None:
            value["retry_at"] = self.retry_at.isoformat() if isinstance(self.retry_at, datetime) else str(self.retry_at)
        return value


def _validate_field(value: object, *, maximum: int, required: bool = False) -> str | None:
    """校验 policy 关联字段，拒绝控制字符、隐式类型转换和超长输入。"""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise OperationPolicyError("operation_grant_invalid")
    if not value or value != value.strip() or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OperationPolicyError("operation_grant_invalid")
    return value


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """policy 使用的可信请求；客户端提交的身份字段不会进入该对象。"""

    scope: ScopeContext
    operation: str
    idempotency_key: str
    resource_id: str | None = None
    task_id: str | None = None
    source: str = "core"
    units: int = 1

    def __post_init__(self) -> None:
        """验证 operation、scope 和服务端幂等键的最小安全约束。"""
        try:
            scope = self.scope if isinstance(self.scope, ScopeContext) else ScopeContext(self.scope)
        except (TypeError, ValueError) as exc:
            raise OperationPolicyError("operation_grant_invalid") from exc
        if self.operation not in Operations.ALL:
            raise OperationPolicyError("operation_unknown")
        idempotency_key = _validate_field(self.idempotency_key, maximum=255, required=True)
        resource_id = _validate_field(self.resource_id, maximum=255)
        task_id = _validate_field(self.task_id, maximum=255)
        source = _validate_field(self.source, maximum=64, required=True)
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise OperationPolicyError("operation_grant_invalid")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "resource_id", resource_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "source", source)


@dataclass(frozen=True, slots=True)
class GrantRef:
    """不透明的服务端 grant 引用；不得序列化到客户端 payload。"""

    grant_id: str
    operation: str
    idempotency_key: str
    scope: ScopeContext

    def __post_init__(self) -> None:
        """拒绝空 grant 和 scope 改绑。"""
        grant_id = _validate_field(self.grant_id, maximum=255, required=True)
        idempotency_key = _validate_field(self.idempotency_key, maximum=255, required=True)
        if self.operation not in Operations.ALL:
            raise OperationPolicyError("operation_grant_invalid")
        if not isinstance(self.scope, ScopeContext):
            try:
                object.__setattr__(self, "scope", ScopeContext(self.scope))
            except (TypeError, ValueError) as exc:
                raise OperationPolicyError("operation_grant_invalid") from exc
        object.__setattr__(self, "grant_id", grant_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """probe/acquire 的稳定结果；拒绝时只保留公开原因和可选提示。"""

    allowed: bool
    reason: str | None = None
    retry_at: datetime | str | None = None
    grant: GrantRef | None = None

    @property
    def granted(self) -> bool:
        """兼容调用方对 acquire 结果的布尔检查。"""
        return self.allowed and self.grant is not None


@dataclass(frozen=True, slots=True)
class GrantResult:
    """commit/release 的幂等结果。"""

    ok: bool
    state: str
    reason: str | None = None
    retry_at: datetime | str | None = None


class OperationPolicy(Protocol):
    """宿主注入的 operation policy 协议。"""

    def probe(self, request: OperationRequest) -> PolicyDecision:
        """返回非权威可用性提示，不得预占 grant。"""

    def acquire(self, request: OperationRequest) -> PolicyDecision:
        """在真实副作用前原子取得 grant 或返回稳定拒绝。"""

    def commit(self, grant: GrantRef) -> GrantResult:
        """确认不可逆副作用已经达到计量点。"""

    def release(self, grant: GrantRef) -> GrantResult:
        """仅在可证明没有发生副作用时释放 reservation。"""


def _decision(value: object, *, grant: GrantRef | None = None) -> PolicyDecision:
    """将宿主 policy 的兼容返回值收敛为稳定结果。"""
    if isinstance(value, PolicyDecision):
        return value
    if isinstance(value, bool):
        return PolicyDecision(value, None if value else "operation_forbidden", grant=grant if value else None)
    if isinstance(value, Mapping):
        allowed = bool(value.get("allowed", value.get("available", False)))
        reason = value.get("reason") or value.get("error")
        reason_text = str(reason) if reason else (None if allowed else "operation_limit_exceeded")
        retry_at = value.get("retry_at")
        candidate = value.get("grant")
        return PolicyDecision(allowed, reason_text, retry_at, candidate if isinstance(candidate, GrantRef) else grant if allowed else None)
    raise OperationPolicyError("operation_policy_unavailable")


def require_allowed(result: PolicyDecision) -> GrantRef:
    """将 acquire 拒绝转换为稳定异常，并返回不可伪造 grant。"""
    if not result.allowed or result.grant is None:
        code = result.reason if result.reason in {"operation_forbidden", "operation_limit_exceeded", "operation_policy_unavailable"} else "operation_policy_unavailable"
        raise OperationPolicyError(code, retry_at=result.retry_at)
    return result.grant


def validate_grant(request: OperationRequest, grant: GrantRef) -> GrantRef:
    """确认宿主返回的 grant 没有被错误绑定到其它 scope 或 operation。"""
    if grant.scope != request.scope or grant.operation != request.operation or grant.idempotency_key != request.idempotency_key:
        raise OperationPolicyError("operation_grant_invalid")
    return grant


class AllowAllOperationPolicy:
    """开源 local 应用显式装配的无额度 policy。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._grants: dict[tuple[str, str, str], GrantRef] = {}
        self._states: dict[str, str] = {}

    def _grant(self, request: OperationRequest) -> GrantRef:
        key = (request.scope.scope_id, request.operation, request.idempotency_key)
        with self._lock:
            current = self._grants.get(key)
            if current is not None:
                return current
            value = GrantRef(secrets.token_urlsafe(24), request.operation, request.idempotency_key, request.scope)
            self._grants[key] = value
            self._states[value.grant_id] = "acquired"
            return value

    def probe(self, request: OperationRequest) -> PolicyDecision:
        """返回允许提示，不创建 grant。"""
        OperationRequest(request.scope, request.operation, request.idempotency_key, request.resource_id, request.task_id, request.source, request.units)
        return PolicyDecision(True)

    def acquire(self, request: OperationRequest) -> PolicyDecision:
        """幂等返回同一 scope/operation/key 的 grant。"""
        grant = self._grant(request)
        return PolicyDecision(True, grant=grant)

    def _finish(self, grant: GrantRef, state: str) -> GrantResult:
        with self._lock:
            if self._grants.get((grant.scope.scope_id, grant.operation, grant.idempotency_key)) != grant:
                raise OperationPolicyError("operation_grant_invalid")
            current = self._states.get(grant.grant_id)
            if current == "unknown":
                return GrantResult(False, current, "operation_grant_invalid")
            if current == state:
                return GrantResult(True, state)
            if current in {"committed", "released"}:
                return GrantResult(True, current)
            self._states[grant.grant_id] = state
            return GrantResult(True, state)

    def commit(self, grant: GrantRef) -> GrantResult:
        """幂等提交已达到计量点的 grant。"""
        return self._finish(grant, "committed")

    def release(self, grant: GrantRef) -> GrantResult:
        """幂等释放尚未发生副作用的 grant。"""
        return self._finish(grant, "released")


class UnavailableOperationPolicy:
    """非 local 宿主未装配 policy 时的 fail-closed 实现。"""

    def probe(self, request: OperationRequest) -> PolicyDecision:
        """返回不可用，不泄露宿主策略细节。"""
        del request
        return PolicyDecision(False, "operation_policy_unavailable")

    def acquire(self, request: OperationRequest) -> PolicyDecision:
        """拒绝所有真实操作。"""
        del request
        return PolicyDecision(False, "operation_policy_unavailable")

    def commit(self, grant: GrantRef) -> GrantResult:
        """未知 policy 下不接受伪造 grant。"""
        del grant
        return GrantResult(False, "unknown", "operation_policy_unavailable")

    def release(self, grant: GrantRef) -> GrantResult:
        """未知 policy 下不释放未知 reservation。"""
        del grant
        return GrantResult(False, "unknown", "operation_policy_unavailable")


class OperationPolicyGateway:
    """校验可信上下文并适配宿主 policy 返回值的核心门面。"""

    def __init__(self, policy: OperationPolicy | None, *, allow_all: bool = False) -> None:
        self.policy = policy if policy is not None else UnavailableOperationPolicy()
        self.allow_all = allow_all

    @staticmethod
    def request(scope: ScopeContext | str, operation: str, idempotency_key: str, **kwargs: Any) -> OperationRequest:
        """从服务端事实构造 policy request，忽略客户端身份字段。"""
        kwargs.pop("scope_id", None)
        kwargs.pop("user_id", None)
        kwargs.pop("grant", None)
        return OperationRequest(scope if isinstance(scope, ScopeContext) else ScopeContext(scope), operation, idempotency_key, **{key: value for key, value in kwargs.items() if key in {"resource_id", "task_id", "source", "units"}})

    def probe(self, request: OperationRequest) -> PolicyDecision:
        """执行非权威可用性查询。"""
        try:
            return _decision(self.policy.probe(request))
        except OperationPolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不泄露宿主策略异常
            raise OperationPolicyError("operation_policy_unavailable") from exc

    def acquire(self, request: OperationRequest) -> PolicyDecision:
        """执行真实副作用前的原子授权。"""
        try:
            decision = _decision(self.policy.acquire(request))
            if decision.grant is not None:
                validate_grant(request, decision.grant)
            return decision
        except OperationPolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不泄露宿主策略异常
            raise OperationPolicyError("operation_policy_unavailable") from exc

    def commit(self, grant: GrantRef) -> GrantResult:
        """提交 grant，并将异常收敛为策略不可用。"""
        try:
            value = self.policy.commit(grant)
            result = value if isinstance(value, GrantResult) else GrantResult(bool(value), "committed" if value else "unknown")
            if result.state not in {"committed", "already_committed", "unknown"}:
                return GrantResult(False, "unknown", "operation_policy_unavailable", result.retry_at)
            if result.ok and result.state not in {"committed", "already_committed"}:
                return GrantResult(False, "unknown", result.reason or "operation_policy_unavailable", result.retry_at)
            return result
        except OperationPolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不泄露宿主策略异常
            raise OperationPolicyError("operation_policy_unavailable") from exc

    def release(self, grant: GrantRef) -> GrantResult:
        """仅由调用方在确认没有副作用后释放 grant。"""
        try:
            value = self.policy.release(grant)
            result = value if isinstance(value, GrantResult) else GrantResult(bool(value), "released" if value else "unknown")
            # 已提交 grant 的 release 只能是幂等 no-op，不能被适配层改写成
            # unknown，否则调用方会误以为可以再次预占或返还已计量额度。
            if result.state not in {"released", "already_released", "committed", "already_committed", "unknown"}:
                return GrantResult(False, "unknown", "operation_policy_unavailable", result.retry_at)
            if result.ok and result.state not in {"released", "already_released", "committed", "already_committed"}:
                return GrantResult(False, "unknown", result.reason or "operation_policy_unavailable", result.retry_at)
            return result
        except OperationPolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不泄露宿主策略异常
            raise OperationPolicyError("operation_policy_unavailable") from exc


@dataclass
class GrantAssociation:
    """服务端内存中的 grant 关联事实；生产宿主可替换为持久化 repository。"""

    request: OperationRequest
    grant: GrantRef
    state: str = "acquired"
    metadata: dict[str, object] = field(default_factory=dict)


class GrantAssociationStore:
    """按 scope/operation/key 保存不透明 grant 关联并提供幂等读取。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._values: dict[tuple[str, str, str], GrantAssociation] = {}

    def get(self, request: OperationRequest) -> GrantAssociation | None:
        """读取当前 scope 的关联，不接受客户端 grant 作为查找键。"""
        with self._lock:
            return self._values.get((request.scope.scope_id, request.operation, request.idempotency_key))

    def put(self, association: GrantAssociation) -> GrantAssociation:
        """幂等写入 grant 关联；冲突时保留最初服务端事实。"""
        key = (association.request.scope.scope_id, association.request.operation, association.request.idempotency_key)
        with self._lock:
            return self._values.setdefault(key, association)

    def acquire(self, request: OperationRequest, gateway: OperationPolicyGateway) -> GrantAssociation:
        """在同一进程内串行化同一幂等键的 acquire，避免并发预占多个 grant。"""
        key = (request.scope.scope_id, request.operation, request.idempotency_key)
        with self._lock:
            existing = self._values.get(key)
            if existing is not None:
                return existing
            grant = validate_grant(request, require_allowed(gateway.acquire(request)))
            association = GrantAssociation(request, grant)
            self._values[key] = association
            return association

    def transition(self, grant: GrantRef, state: str) -> bool:
        """按不透明引用幂等收束关联状态，并拒绝跨 scope 或伪造引用。"""
        if state not in {"committed", "released", "unknown"}:
            raise OperationPolicyError("operation_grant_invalid")
        with self._lock:
            key = (grant.scope.scope_id, grant.operation, grant.idempotency_key)
            association = self._values.get(key)
            if association is None or association.grant != grant:
                return False
            if association.state in {"committed", "released", "unknown"}:
                return association.state == state
            association.state = state
            return True

    def bind_task(self, grant: GrantRef, task_id: str) -> bool:
        """把已取得的 grant 绑定到实际叶子 Task；内存 policy 只记录受信摘要。"""
        task_id = _validate_field(task_id, maximum=255, required=True)
        with self._lock:
            key = (grant.scope.scope_id, grant.operation, grant.idempotency_key)
            association = self._values.get(key)
            if association is None or association.grant != grant:
                return False
            association.metadata["task_id"] = task_id
            return True


class PersistentGrantAssociationStore:
    """按请求 scope 路由到 PostgreSQL grant 关联，并保留进程内快速缓存。"""

    def __init__(self, resources: Any):
        self.resources = resources
        self._memory = GrantAssociationStore()
        self._lock = RLock()

    @staticmethod
    def _repository(resources: Any, scope: ScopeContext) -> "PersistentGrantRepository":
        """构造一次性 scope-bound 持久 repository。"""
        return PersistentGrantRepository(resources, scope)

    def get(self, request: OperationRequest) -> GrantAssociation | None:
        """先查进程缓存，再查持久事实；跨 scope 不共享关联。"""
        cached = self._memory.get(request)
        if cached is not None:
            return cached
        value = self._repository(self.resources, request.scope).get(request)
        if value is not None:
            self._memory.put(value)
        return value

    def put(self, association: GrantAssociation) -> GrantAssociation:
        """持久写入并同步内存缓存。"""
        value = self._repository(self.resources, association.request.scope).put(association)
        return self._memory.put(value)

    def acquire(self, request: OperationRequest, gateway: OperationPolicyGateway) -> GrantAssociation:
        """在数据库 advisory lock 下完成跨进程幂等 acquire。"""
        with self._lock:
            cached = self.get(request)
            if cached is not None:
                return cached
            value = self._repository(self.resources, request.scope).acquire(request, gateway)
            return self._memory.put(value)

    def transition(self, grant: GrantRef, state: str) -> bool:
        """持久且幂等地提交 grant 状态，再更新进程缓存。"""
        changed = self._repository(self.resources, grant.scope).transition(grant, state)
        if changed:
            self._memory.transition(grant, state)
        return changed

    def bind_task(self, grant: GrantRef, task_id: str) -> bool:
        """在持久 grant 事实中绑定当前 scope 的实际叶子 Task。"""
        changed = self._repository(self.resources, grant.scope).bind_task(grant, task_id)
        if changed:
            self._memory.bind_task(grant, task_id)
        return changed


class PersistentGrantRepository:
    """将 grant 关联写入 PostgreSQL 的 scope-safe repository。"""

    def __init__(self, resources: Any, scope: ScopeContext | str):
        self.resources = resources
        self.scope = scope if isinstance(scope, ScopeContext) else ScopeContext(scope)

    def get(self, request: OperationRequest) -> GrantAssociation | None:
        """按服务端 scope/operation/key 幂等读取 grant。"""
        if request.scope != self.scope:
            raise OperationPolicyError("operation_grant_invalid")
        with self.resources.factory() as session:
            row = session.get(OperationGrant, (self.scope.scope_id, request.operation, request.idempotency_key))
            if row is None:
                return None
            grant = GrantRef(row.grant_id, row.operation, row.idempotency_key, self.scope)
            return GrantAssociation(request, grant, row.state, {"attempt_id": row.attempt_id, "input_digest": row.input_digest})

    def put(self, association: GrantAssociation) -> GrantAssociation:
        """原子保存 grant 关联；重复 key 返回最初事实。"""
        request, grant = association.request, association.grant
        if request.scope != self.scope or grant.scope != self.scope:
            raise OperationPolicyError("operation_grant_invalid")
        with self.resources.factory() as session:
            row = session.get(OperationGrant, (self.scope.scope_id, request.operation, request.idempotency_key), with_for_update=True)
            if row is None:
                row = OperationGrant(scope_id=self.scope.scope_id, operation=request.operation, idempotency_key=request.idempotency_key, grant_id=grant.grant_id, task_id=request.task_id, resource_id=request.resource_id, state=association.state, attempt_id=association.metadata.get("attempt_id"), input_digest=association.metadata.get("input_digest"))
                session.add(row)
                session.flush()
            session.commit()
            return GrantAssociation(request, GrantRef(row.grant_id, row.operation, row.idempotency_key, self.scope), row.state, association.metadata)

    def acquire(self, request: OperationRequest, gateway: OperationPolicyGateway) -> GrantAssociation:
        """在 scope/operation/key advisory lock 下只向宿主 policy acquire 一次。"""
        if request.scope != self.scope:
            raise OperationPolicyError("operation_grant_invalid")
        key = f"mememeow:grant:{self.scope.scope_id}:{request.operation}:{request.idempotency_key}"
        with self.resources.factory() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
            row = session.get(OperationGrant, (self.scope.scope_id, request.operation, request.idempotency_key), with_for_update=True)
            if row is not None:
                session.commit()
                return GrantAssociation(request, GrantRef(row.grant_id, row.operation, row.idempotency_key, self.scope), row.state, {"attempt_id": row.attempt_id, "input_digest": row.input_digest})
            grant = require_allowed(gateway.acquire(request))
            row = OperationGrant(
                scope_id=self.scope.scope_id,
                operation=request.operation,
                idempotency_key=request.idempotency_key,
                grant_id=grant.grant_id,
                task_id=request.task_id,
                resource_id=request.resource_id,
                state="acquired",
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                existing = session.get(OperationGrant, (self.scope.scope_id, request.operation, request.idempotency_key))
                if existing is None:
                    raise OperationPolicyError("operation_policy_unavailable") from exc
                row = existing
            return GrantAssociation(request, GrantRef(row.grant_id, row.operation, row.idempotency_key, self.scope), row.state)

    def transition(self, grant: GrantRef, state: str) -> bool:
        """按不透明 grant id 幂等更新 commit/release 状态。"""
        if state not in {"committed", "released", "unknown"}:
            raise OperationPolicyError("operation_grant_invalid")
        if grant.scope != self.scope:
            raise OperationPolicyError("operation_grant_invalid")
        with self.resources.factory() as session:
            row = session.scalar(
                select(OperationGrant)
                .where(
                    OperationGrant.scope_id == self.scope.scope_id,
                    OperationGrant.grant_id == grant.grant_id,
                    OperationGrant.operation == grant.operation,
                    OperationGrant.idempotency_key == grant.idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                session.commit()
                return False
            if row.state in {"committed", "released", "unknown"}:
                session.commit()
                return row.state == state
            row.state = state
            row.updated_at = utcnow()
            session.commit()
            return True

    def bind_task(self, grant: GrantRef, task_id: str) -> bool:
        """按 grant 引用绑定已提交的 scope-safe Task ID，并拒绝改绑。"""
        task_id = _validate_field(task_id, maximum=255, required=True)
        if grant.scope != self.scope:
            raise OperationPolicyError("operation_grant_invalid")
        with self.resources.factory() as session:
            try:
                row = session.scalar(
                    select(OperationGrant)
                    .where(
                        OperationGrant.scope_id == self.scope.scope_id,
                        OperationGrant.grant_id == grant.grant_id,
                        OperationGrant.operation == grant.operation,
                        OperationGrant.idempotency_key == grant.idempotency_key,
                    )
                    .with_for_update()
                )
                if row is None:
                    session.commit()
                    return False
                if row.task_id not in {None, task_id}:
                    session.commit()
                    return False
                task = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id))
                if task is None or row.state != "acquired":
                    session.commit()
                    return False
                row.task_id = task_id
                row.updated_at = utcnow()
                session.commit()
                return True
            except IntegrityError as exc:
                session.rollback()
                raise OperationPolicyError("operation_grant_invalid") from exc
