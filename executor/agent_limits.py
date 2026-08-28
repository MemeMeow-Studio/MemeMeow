"""Agent lane 的公共运行容量校验。

该模块位于 executor 公共快照中，后端和独立 executor 共用同一套容量语义。运行
并发只要求是正整数；全局、scope 和资源池之间的层级关系由调用方显式校验。Agent
队列不使用有限的背压容量作为拒绝门禁，具体部署资源由适配层和运行环境负责。
"""

from __future__ import annotations

from typing import Final


AGENT_BACKPRESSURE_DEFAULT: Final = 80


def validate_agent_backpressure(value: object) -> int:
    """校验旧 Agent 背压配置，供兼容读取使用。

    该字段不再参与 Agent 运行并发或队列拒绝；保留校验函数是为了让旧部署和外部
    调用方在迁移期间仍能读取其正整数配置，而不是把该值伪装成无限队列容量。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("agent_backpressure_invalid")
    if value < 1:
        raise ValueError("agent_backpressure_out_of_range")
    return value


def validate_agent_concurrency(value: object, *, backpressure: object | None = None) -> int:
    """校验 Agent 运行并发并返回原始整数。

    ``backpressure`` 仅为旧调用方保留，不再参与校验；Agent 队列没有有限容量门禁。
    需要表达 scope 或资源池不超过上层容量时，调用
    :func:`validate_agent_concurrency_at_most`，避免误把队列预算当作运行上限。
    """
    del backpressure
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("agent_concurrency_invalid")
    return value


def validate_agent_concurrency_at_most(
    value: object,
    maximum: object,
    *,
    error_code: str = "agent_concurrency_exceeds_limit",
) -> int:
    """校验运行并发为正整数且不超过显式的上层容量。

    ``value`` 通常是 scope 或资源池容量，``maximum`` 是全局 Agent 或 lane 容量；
    该关系只限制可运行任务数量，不限制 queued 任务数量。调用方可通过
    ``error_code`` 保持配置层和数据库层的稳定错误边界。
    """
    candidate = validate_agent_concurrency(value)
    ceiling = validate_agent_concurrency(maximum)
    if candidate > ceiling:
        raise ValueError(error_code)
    return candidate
