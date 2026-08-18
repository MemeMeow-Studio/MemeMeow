"""operation policy 公共契约的无数据库单元测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.database import ScopeContext
from backend.operation_policy import (
    AllowAllOperationPolicy,
    GrantAssociation,
    GrantResult,
    GrantRef,
    GrantAssociationStore,
    OperationPolicyError,
    OperationPolicyGateway,
    OperationRequest,
    Operations,
    PolicyDecision,
    PersistentGrantAssociationStore,
    require_allowed,
    validate_grant,
)


def test_allow_all_grant_is_idempotent_and_scope_bound() -> None:
    """开源 allow-all 对同一 scope/操作/幂等键只发行一个 grant。"""
    scope = ScopeContext("local")
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    request = gateway.request(scope, Operations.IMAGE_UPLOAD, "upload:one")

    first = gateway.acquire(request)
    second = gateway.acquire(request)

    assert first.grant is not None
    assert second.grant == first.grant
    assert validate_grant(request, first.grant) == first.grant
    assert gateway.commit(first.grant).ok
    assert gateway.commit(first.grant).state == "committed"


def test_grant_ref_rejects_malformed_binding() -> None:
    """grant 引用缺少操作或幂等键时必须在边界处拒绝。"""
    with pytest.raises(OperationPolicyError) as missing_operation:
        GrantRef("grant", "not-an-operation", "key", ScopeContext("local"))
    assert missing_operation.value.code == "operation_grant_invalid"

    with pytest.raises(OperationPolicyError) as missing_key:
        GrantRef("grant", Operations.IMAGE_UPLOAD, "", ScopeContext("local"))
    assert missing_key.value.code == "operation_grant_invalid"


def test_grant_validation_rejects_scope_or_operation_rebinding() -> None:
    """已发行 grant 不能被改绑到其它 scope、操作或幂等键。"""
    scope = ScopeContext("local")
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    request = gateway.request(scope, Operations.IMAGE_UPLOAD, "upload:one")
    grant = gateway.acquire(request).grant
    assert grant is not None

    with pytest.raises(OperationPolicyError):
        validate_grant(gateway.request("other", Operations.IMAGE_UPLOAD, "upload:one"), grant)
    with pytest.raises(OperationPolicyError):
        validate_grant(gateway.request(scope, Operations.IMAGE_DELETE, "upload:one"), grant)
    with pytest.raises(OperationPolicyError):
        validate_grant(gateway.request(scope, Operations.IMAGE_UPLOAD, "upload:two"), grant)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"idempotency_key": "bad\nkey"},
        {"idempotency_key": " key"},
        {"units": True},
        {"units": 1.5},
        {"source": "worker\x00"},
    ),
)
def test_operation_request_rejects_ambiguous_field_types_and_controls(kwargs: dict[str, object]) -> None:
    """policy 关联字段不接受控制字符、隐式类型转换或首尾空白。"""
    with pytest.raises(OperationPolicyError, match="操作授权无效"):
        arguments = {"scope": ScopeContext("local"), "operation": Operations.IMAGE_UPLOAD, "idempotency_key": "upload:one"}
        arguments.update(kwargs)
        OperationRequest(**arguments)


class _WrongCommitStatePolicy(AllowAllOperationPolicy):
    """返回错误 commit 状态的宿主策略夹具。"""

    def commit(self, grant):
        """模拟策略错误地把 acquired 当作已提交。"""
        del grant
        return GrantResult(True, "acquired")


def test_gateway_rejects_successful_commit_with_non_terminal_state() -> None:
    """核心不得把非 committed 状态当成已达到计量点。"""
    scope = ScopeContext("local")
    gateway = OperationPolicyGateway(_WrongCommitStatePolicy())
    request = gateway.request(scope, Operations.IMAGE_UPLOAD, "upload:wrong-state")
    grant = gateway.acquire(request).grant
    assert grant is not None
    result = gateway.commit(grant)
    assert result.ok is False
    assert result.state == "unknown"


class _DenyPolicy(AllowAllOperationPolicy):
    """返回稳定拒绝的宿主策略夹具。"""

    def probe(self, request):
        """probe 只返回拒绝提示，不创建 grant。"""
        return PolicyDecision(False, "operation_limit_exceeded")

    def acquire(self, request):
        """acquire 模拟宿主拒绝最后一个 reservation。"""
        return PolicyDecision(False, "operation_forbidden")


class _BrokenPolicy(AllowAllOperationPolicy):
    """抛出未公开异常的策略夹具。"""

    def acquire(self, request):
        """模拟策略服务故障。"""
        raise RuntimeError("private quota payload")


def test_probe_and_acquire_denials_fail_closed_without_grant_or_scope_leak() -> None:
    """拒绝和策略异常只暴露稳定错误，空 resource_id 不构成伪造授权。"""
    scope = ScopeContext("tenant-policy")
    gateway = OperationPolicyGateway(_DenyPolicy())
    request = gateway.request(scope, Operations.IMAGE_UPLOAD, "upload:deny", resource_id=None)
    decision = gateway.probe(request)
    assert decision.allowed is False
    assert decision.grant is None
    with pytest.raises(OperationPolicyError) as denied:
        require_allowed(gateway.acquire(request))
    assert denied.value.code == "operation_forbidden"
    assert "tenant-policy" not in str(denied.value)

    broken = OperationPolicyGateway(_BrokenPolicy())
    with pytest.raises(OperationPolicyError) as unavailable:
        broken.acquire(request)
    assert unavailable.value.code == "operation_policy_unavailable"
    assert "private quota payload" not in str(unavailable.value)


def test_concurrent_same_key_acquire_creates_one_grant() -> None:
    """同一 scope/operation/idempotency key 并发 acquire 只返回一个 grant。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = GrantAssociationStore()
    request = gateway.request(ScopeContext("tenant-concurrent"), Operations.ANALYSIS_AGENT, "agent:same", resource_id=None)

    def acquire_once():
        """执行一次并发 reservation。"""
        return store.acquire(request, gateway).grant.grant_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _index: acquire_once(), range(32)))
    assert len(set(values)) == 1


def test_grant_association_reuses_only_identical_server_facts() -> None:
    """同一事实可复用 grant，resource/task/source/units/input 摘要冲突必须拒绝。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = GrantAssociationStore()
    base = gateway.request(
        "tenant-facts",
        Operations.ANALYSIS_AGENT,
        "agent:facts",
        resource_id="meme-1",
        task_id="task-1",
        source="image-processing",
        units=1,
        input_digest="a" * 64,
    )
    association = store.acquire(base, gateway)
    assert store.acquire(base, gateway).grant == association.grant
    for mutation in (
        {"resource_id": "meme-2"},
        {"task_id": "task-2"},
        {"source": "other"},
        {"units": 2},
        {"input_digest": "b" * 64},
    ):
        conflicting = gateway.request(
            "tenant-facts",
            Operations.ANALYSIS_AGENT,
            "agent:facts",
            resource_id=mutation.get("resource_id", base.resource_id),
            task_id=mutation.get("task_id", base.task_id),
            source=mutation.get("source", base.source),
            units=mutation.get("units", base.units),
            input_digest=mutation.get("input_digest", base.input_digest),
        )
        with pytest.raises(OperationPolicyError) as error:
            store.acquire(conflicting, gateway)
        assert error.value.code == "operation_policy_unavailable"


@pytest.mark.parametrize("terminal_state", ("committed", "released", "unknown"))
def test_terminal_association_cannot_be_reused_as_execution_grant(terminal_state: str) -> None:
    """已提交、已释放和未知关联只能用于恢复观察，不能再次 acquire。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = GrantAssociationStore()
    request = gateway.request("tenant-terminal", Operations.IMAGE_UPLOAD, f"upload:{terminal_state}", input_digest="a" * 64)
    association = store.acquire(request, gateway)
    assert store.transition(association.grant, terminal_state) is True
    observed = store.get(request)
    assert observed is not None and observed.state == terminal_state
    with pytest.raises(OperationPolicyError) as error:
        store.acquire(request, gateway)
    assert error.value.code == "operation_policy_unavailable"


def test_pipeline_task_binding_updates_the_persisted_request_fingerprint() -> None:
    """pipeline 先取得 grant 后绑定叶子 Task 时，新的可信事实仍可幂等命中。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = GrantAssociationStore()
    initial = gateway.request("tenant-pipeline", Operations.ANALYSIS_AGENT, "agent:pipeline", resource_id="meme-1", source="image-processing", input_digest="a" * 64)
    association = store.acquire(initial, gateway)
    assert store.bind_task(association.grant, "task-1") is True
    bound = gateway.request("tenant-pipeline", Operations.ANALYSIS_AGENT, "agent:pipeline", resource_id="meme-1", task_id="task-1", source="image-processing", input_digest="a" * 64)
    assert store.get(bound) is association
    with pytest.raises(OperationPolicyError):
        store.get(initial)


def test_commit_and_release_are_idempotent_without_refunding_committed_grant() -> None:
    """commit/release 重复调用稳定收束，已 commit 的 grant 不会被 release 改写。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = GrantAssociationStore()
    request = gateway.request(ScopeContext("tenant-lifecycle"), Operations.IMAGE_DELETE, "delete:one")
    association = store.acquire(request, gateway)
    assert gateway.commit(association.grant).state == "committed"
    assert gateway.commit(association.grant).state == "committed"
    assert gateway.release(association.grant).state == "committed"
    assert store.transition(association.grant, "committed") is True
    assert store.transition(association.grant, "committed") is True
    assert store.transition(association.grant, "released") is False


class _MutablePersistentGrantRepository:
    """用可变持久事实模拟另一个进程收束 grant 的最小 repository。"""

    def __init__(self, association: GrantAssociation) -> None:
        """保存当前持久状态，并让每次读取返回新的关联对象。"""
        self.association = association
        self.get_calls = 0
        self.acquire_calls = 0

    def get(self, request: OperationRequest) -> GrantAssociation:
        """返回当前持久状态，避免测试意外共享进程缓存对象。"""
        self.get_calls += 1
        current = self.association
        return GrantAssociation(request, current.grant, current.state, current.metadata)

    def put(self, association: GrantAssociation) -> GrantAssociation:
        """写入测试用的持久关联。"""
        self.association = association
        return association

    def acquire(self, request: OperationRequest, gateway: OperationPolicyGateway) -> GrantAssociation:
        """模拟持久层拒绝已收束关联，证明 acquire 不使用旧缓存旁路。"""
        self.acquire_calls += 1
        current = self.get(request)
        if current.state != "acquired":
            raise OperationPolicyError("operation_policy_unavailable")
        return current


def test_persistent_store_never_uses_stale_terminal_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨进程将 grant 收束后，持久 store 必须读取新终态而非返回旧 acquired 缓存。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    request = gateway.request("tenant-cache", Operations.IMAGE_UPLOAD, "upload:cache", source="test")
    grant = gateway.acquire(request).grant
    assert grant is not None
    acquired = GrantAssociation(request, grant)
    repository = _MutablePersistentGrantRepository(acquired)
    store = PersistentGrantAssociationStore(object())
    monkeypatch.setattr(store, "_repository", lambda _resources, _scope: repository)
    store.put(acquired)

    # 模拟另一个进程在数据库中将相同 reservation 标记为 released。
    repository.association = GrantAssociation(request, grant, "released")
    observed = store.get(request)
    assert observed is not None and observed.state == "released"
    assert repository.get_calls == 1

    with pytest.raises(OperationPolicyError) as error:
        store.acquire(request, gateway)
    assert error.value.code == "operation_policy_unavailable"
    assert repository.acquire_calls == 1


def test_gateway_rejects_client_grant_and_scope_overrides() -> None:
    """gateway.request 丢弃客户端身份字段，伪造 grant 不能改变可信 request。"""
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    request = gateway.request(
        "tenant-trusted",
        Operations.IMAGE_UPLOAD,
        "upload:trusted",
        scope_id="attacker",
        user_id="attacker",
        grant="forged",
        resource_id=None,
    )
    assert request.scope.scope_id == "tenant-trusted"
    assert request.resource_id is None
