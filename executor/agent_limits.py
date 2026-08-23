"""Agent lane 的公共容量校验。

该模块位于 executor 公共快照中，后端和独立 executor 共用同一套背压安全边界。
并发本身不绑定某个产品规模；实际并发必须是正整数，且不能超过部署配置的 Agent
背压容量。背压仍保留一个较高的实现级上限，用于防止错误配置把线程池和任务队列
扩张到不可控大小。
"""

from __future__ import annotations

from typing import Final


AGENT_BACKPRESSURE_DEFAULT: Final = 80
# 这是资源保护上限，不是产品并发配额；Server 等部署层可以继续施加更低的门禁。
AGENT_BACKPRESSURE_SAFETY_MAX: Final = 500


def validate_agent_backpressure(value: object) -> int:
    """校验 Agent 背压容量并返回原始整数。

    背压容量决定排队资源预算，调用场景是配置加载、任务服务和 executor 启动。
    非正数、布尔值、非整数或超过实现级资源上限的输入都会显式失败，不做静默截断。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("agent_backpressure_invalid")
    if not 1 <= value <= AGENT_BACKPRESSURE_SAFETY_MAX:
        raise ValueError("agent_backpressure_out_of_range")
    return value


def validate_agent_concurrency(value: object, *, backpressure: object | None = None) -> int:
    """校验 Agent 并发并返回原始整数。

    并发是部署配置而非公共核心的固定产品规模。调用场景是 Settings、API、调度器
    和线程池创建；当提供背压容量时，并发不得超过该容量，从而让不同执行层都拥有
    明确的资源预算。任何越界输入都抛出稳定错误，禁止隐式转换或截断。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("agent_concurrency_invalid")
    if value > AGENT_BACKPRESSURE_SAFETY_MAX:
        raise ValueError("agent_concurrency_safety_limit")
    if backpressure is not None:
        pressure = validate_agent_backpressure(backpressure)
        if value > pressure:
            raise ValueError("agent_concurrency_exceeds_backpressure")
    return value
