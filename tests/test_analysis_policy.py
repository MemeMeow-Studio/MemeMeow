"""公共分析金额策略的校验、默认关闭和冻结快照测试。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from executor.analysis_policy import AnalysisPolicyError, freeze_analysis_policy, parse_analysis_policy


def freeze(**kwargs):
    """使用固定模型身份构建策略，测试不依赖任何部署目录。"""
    return freeze_analysis_policy(model_key="test", model="provider/model", variant="max", **kwargs)


def test_disabled_by_default():
    """未启用时不要求任何金额配置。"""
    assert freeze() is None


@pytest.mark.parametrize("reminder,termination", [("0.05", "0.10"), ("0.10", "0.20"), ("0.15", "0.30")])
def test_default_double(reminder, termination):
    """配置生成器将省略的终止金额展开为明确快照。"""
    snapshot = freeze(enabled=True, reminder_cost=reminder)
    policy = parse_analysis_policy(snapshot)
    assert policy.reminder_cost == Decimal(reminder)
    assert policy.termination_cost == Decimal(termination)
    assert policy.model == "provider/model"


def test_explicit_ratio_and_immutable_snapshot():
    """显式非两倍限额保持不变，解析结果不可修改。"""
    snapshot = freeze(enabled=True, reminder_cost="0.4", termination_cost="1")
    policy = parse_analysis_policy(snapshot)
    snapshot["termination_cost"] = "9"
    assert policy.termination_cost == Decimal("1")
    with pytest.raises(ValidationError):
        policy.termination_cost = Decimal("9")


@pytest.mark.parametrize("amount", [True, False, 0, -1, "NaN", "Infinity", float("inf"), "bad", [], {}])
def test_invalid_amount(amount):
    """非法金额不能变成无限分析额度。"""
    with pytest.raises(AnalysisPolicyError) as error:
        freeze(enabled=True, reminder_cost=amount)
    assert error.value.code == "agent_analysis_policy_invalid"


@pytest.mark.parametrize("termination", ["0.1", "0.05", "NaN", True])
def test_invalid_termination(termination):
    """终止限额必须有效且严格大于提醒金额。"""
    with pytest.raises(AnalysisPolicyError):
        freeze(enabled=True, reminder_cost="0.1", termination_cost=termination)


def test_missing_policy_and_incomplete_snapshot():
    """恢复只接受完整冻结值，不能现场计算缺失的终止金额。"""
    with pytest.raises(AnalysisPolicyError) as error:
        parse_analysis_policy(None)
    assert error.value.code == "agent_analysis_policy_missing"
    snapshot = freeze(enabled=True, reminder_cost="0.1")
    del snapshot["termination_cost"]
    with pytest.raises(AnalysisPolicyError) as error:
        parse_analysis_policy(snapshot)
    assert error.value.code == "agent_analysis_policy_invalid"
