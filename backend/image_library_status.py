# 图片库状态筛选使用与文本、视觉状态投影相同的数据库条件。

from typing import Literal

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from backend.persistence.models import Meme


ImageStatus = Literal["all", "ready", "unready", "context_unready", "text_unready", "visual_unready"]


def image_status_condition(environment, status: ImageStatus, *, embedding_model: str, visual_identity) -> ColumnElement[bool] | None:
    """根据三项就绪条件生成 SQL 筛选，供图片列表和总数查询共同使用。"""
    if status == "all":
        return None
    context_ready = Meme.context_status == "ready"
    if status == "context_unready":
        return ~context_ready
    text_ready = environment.search.text_embedding_ready_clause(embedding_model)
    visual_ready = environment.visual.visual_embedding_ready_clause(
        model=visual_identity.model,
        preprocess_version=visual_identity.preprocess_version,
        dimensions=visual_identity.dimensions,
    )
    if status == "text_unready":
        return ~text_ready
    if status == "visual_unready":
        return ~visual_ready
    ready = and_(context_ready, text_ready, visual_ready)
    if status == "ready":
        return ready
    if status == "unready":
        return ~ready
    raise ValueError("invalid_image_status")
