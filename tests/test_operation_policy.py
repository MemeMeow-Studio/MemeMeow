"""operation policy 公共契约的无数据库单元测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.database import ScopeContext
from backend.operation_policy import (
    AllowAllOperationPolicy,
    GrantResult,
    GrantRef,
    GrantAssociationStore,
    OperationPolicyError,
    OperationPolicyGateway,
    OperationRequest,
    Operations,
    PolicyDecision,
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
