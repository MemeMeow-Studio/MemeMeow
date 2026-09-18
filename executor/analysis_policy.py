"""公共 Agent 分析用量策略；只处理可信配置，不提供默认模型等级。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class AnalysisPolicyError(ValueError):
    """策略配置错误，供提交层和执行器保留稳定原因。"""

    def __init__(self, code: str, message: str):
        """保存稳定错误码和不含凭据的配置说明。"""
        super().__init__(message)
        self.code = code


class AnalysisPolicy(BaseModel):
    """启用任务的不可变快照，绑定模型身份及两个明确的美元限额。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    version: Literal[1] = 1
    model_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    reminder_cost: Decimal
    termination_cost: Decimal

    @field_validator("reminder_cost", "termination_cost", mode="before")
    @classmethod
    def positive_amount(cls, value: object) -> Decimal:
        """校验配置或快照中的美元金额，拒绝布尔值、非有限值和非正数。"""
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("金额必须为有效正数")
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("金额必须为有效正数") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("金额必须为有限正数")
        return amount

    @model_validator(mode="after")
    def ordered_amounts(self) -> AnalysisPolicy:
        """确保冻结策略为提醒之后留有收尾空间。"""
        if self.termination_cost <= self.reminder_cost:
            raise ValueError("终止金额必须大于提醒金额")
        return self


def parse_analysis_policy(value: object) -> AnalysisPolicy:
    """解析启用任务的完整快照；恢复时不补默认值或重选模型。"""
    if value is None:
        raise AnalysisPolicyError("agent_analysis_policy_missing", "启用分析用量控制但缺少冻结策略")
    try:
        return AnalysisPolicy.model_validate(value)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise AnalysisPolicyError("agent_analysis_policy_invalid", f"分析用量策略无效，字段：{fields or '金额关系'}") from exc


def freeze_analysis_policy(
    *, enabled: bool = False, model_key: str, model: str, variant: str,
    reminder_cost: object = None, termination_cost: object = None,
) -> dict[str, object] | None:
    """从可信配置生成任务快照；默认关闭，配置工具省略终止金额时使用两倍提醒值。"""
    if not isinstance(enabled, bool):
        raise AnalysisPolicyError("agent_analysis_policy_invalid", "启用状态必须为布尔值")
    if not enabled:
        return None
    if reminder_cost is None:
        raise AnalysisPolicyError("agent_analysis_policy_missing", "启用分析用量控制但缺少提醒金额")
    if termination_cost is None:
        try:
            termination_cost = AnalysisPolicy.positive_amount(reminder_cost) * 2
        except ValueError as exc:
            raise AnalysisPolicyError("agent_analysis_policy_invalid", "提醒金额必须为有限正数") from exc
    return parse_analysis_policy({
        "model_key": model_key, "model": model, "variant": variant,
        "reminder_cost": reminder_cost, "termination_cost": termination_cost,
    }).model_dump(mode="json")
