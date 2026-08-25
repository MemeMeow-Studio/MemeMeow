"""图片处理固定阶段计划。

本模块只描述阶段顺序、叶子任务类型和阶段状态判定，不创建数据库任务、不调用
模型，也不负责后续阶段副作用。图片 Job repository 和 Worker facade 通过这里的
窄接口共享同一套推进规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


IMAGE_STAGE_ORDER = ("visual", "agent", "auto_rename", "text_embedding")
CORE_IMAGE_STAGES = ("visual", "agent", "text_embedding")
STAGE_TASK_TYPES = {
    "visual": "visual_embedding_generation",
    "agent": "meme_context_generation",
    "auto_rename": "image_auto_rename",
    "text_embedding": "text_embedding_generation",
}
TASK_TYPE_STAGES = {task_type: stage for stage, task_type in STAGE_TASK_TYPES.items()}
IMAGE_PROCESSING_TASK_TYPES = frozenset(STAGE_TASK_TYPES.values())
IMAGE_PROCESSING_MAX_ATTEMPTS = 1
SETTLED_STAGE_STATUSES = frozenset({"succeeded", "skipped", "warning"})
BLOCKING_STAGE_STATUSES = frozenset({"failed", "blocked", "unknown_execution", "target_changed"})


class ImageStagePlanError(ValueError):
    """阶段名称或状态不符合固定图片处理协议。"""

    def __init__(self, code: str) -> None:
        """创建带稳定错误码的阶段计划错误。"""
        self.code = code
        super().__init__(code)


def normalize_stage(value: object) -> str:
    """规范化公开或内部阶段名称。"""
    if not isinstance(value, str) or value not in STAGE_TASK_TYPES:
        raise ImageStagePlanError("invalid_stage")
    return value


def task_type_for_stage(stage: object) -> str:
    """返回阶段对应的叶子 task type。"""
    return STAGE_TASK_TYPES[normalize_stage(stage)]


def stage_for_task_type(task_type: object) -> str:
    """返回叶子 task type 对应阶段，未知类型 fail-closed。"""
    if not isinstance(task_type, str) or task_type not in TASK_TYPE_STAGES:
        raise ImageStagePlanError("invalid_task_type")
    return TASK_TYPE_STAGES[task_type]


def image_task_requires_single_attempt(
    task_type: object,
    *,
    submission_mode: str | None = None,
    image_stage: str | None = None,
) -> bool:
    """判断图片叶子 Task 是否禁止同一逻辑 task 内自动重放 provider。

    来源字段为空时保留旧任务兼容语义；新 pipeline/standalone 图片任务或明确阶段
    字段必须使用显式重试创建新 Task，避免未知外部副作用被隐式重放。
    """
    if task_type not in IMAGE_PROCESSING_TASK_TYPES:
        return False
    return submission_mode in {"pipeline", "standalone"} or image_stage in TASK_TYPE_STAGES.values()


@dataclass(frozen=True)
class ImageStagePlan:
    """固定顺序的图片阶段编排规则。

    ``auto_name`` 只影响中间 warning/skipped 阶段，不改变核心 visual/agent/text
    顺序；Worker 可据此判断下一个唯一可执行阶段。
    """

    auto_name: bool = False

    def __post_init__(self) -> None:
        """校验自动命名开关的类型，避免客户端值影响阶段顺序。"""
        if type(self.auto_name) is not bool:
            raise ImageStagePlanError("invalid_auto_name")

    @property
    def stages(self) -> tuple[str, ...]:
        """返回完整固定阶段顺序。"""
        return IMAGE_STAGE_ORDER

    def is_enabled(self, stage: object) -> bool:
        """判断阶段是否需要执行；关闭自动命名时该阶段直接跳过。"""
        return normalize_stage(stage) != "auto_rename" or self.auto_name

    def next_stage(self, statuses: Mapping[str, str]) -> str | None:
        """根据已观察阶段状态返回唯一下一阶段。

        前置阶段失败、阻止或未知时返回 ``None``，由调用方保留 Job 终态；不会
        越过失败阶段安排后续任务。
        """
        for stage in self.stages:
            if not self.is_enabled(stage):
                continue
            status = statuses.get(stage, "queued")
            if status in BLOCKING_STAGE_STATUSES:
                return None
            if status not in SETTLED_STAGE_STATUSES:
                return stage
        return None

    def can_run(self, stage: object, statuses: Mapping[str, str]) -> bool:
        """判断某阶段是否是当前唯一可运行阶段。"""
        normalized = normalize_stage(stage)
        return self.next_stage(statuses) == normalized

    def settled(self, statuses: Mapping[str, str]) -> bool:
        """判断所有启用阶段是否已经成功、跳过或 warning 收束。"""
        return all(not self.is_enabled(stage) or statuses.get(stage) in SETTLED_STAGE_STATUSES for stage in self.stages)

    def blocked(self, statuses: Mapping[str, str]) -> bool:
        """判断是否存在阻止继续推进的阶段终态。"""
        return any(statuses.get(stage) in BLOCKING_STAGE_STATUSES for stage in self.stages if self.is_enabled(stage))


def downstream_stages(stage: object) -> tuple[str, ...]:
    """返回指定阶段之后的阶段，供失败收束清理或状态展示使用。"""
    normalized = normalize_stage(stage)
    index = IMAGE_STAGE_ORDER.index(normalized)
    return IMAGE_STAGE_ORDER[index + 1 :]


__all__ = [
    "BLOCKING_STAGE_STATUSES",
    "CORE_IMAGE_STAGES",
    "IMAGE_STAGE_ORDER",
    "IMAGE_PROCESSING_MAX_ATTEMPTS",
    "IMAGE_PROCESSING_TASK_TYPES",
    "ImageStagePlan",
    "ImageStagePlanError",
    "SETTLED_STAGE_STATUSES",
    "STAGE_TASK_TYPES",
    "TASK_TYPE_STAGES",
    "downstream_stages",
    "image_task_requires_single_attempt",
    "normalize_stage",
    "stage_for_task_type",
    "task_type_for_stage",
]
