"""scope 绑定的持久化 Repository 子包。

图片记录、合集关系、文本检索、视觉向量和任务持久化事实分别由独立模块实现；
backend.database 继续提供历史兼容导出。
"""

from backend.persistence.repositories.callbacks import (
    AgentCallbackRequestRepository,
    InMemoryAgentCallbackRequestRepository,
    InMemoryCallbackRequest,
    InMemoryCallbackRequestRepository,
)
from backend.persistence.repositories.collections import CollectionRepository
from backend.persistence.repositories.memes import MemeRepository
from backend.persistence.repositories.reverse_image import ReverseImageUsageRepository
from backend.persistence.repositories.search import SearchRepository
from backend.persistence.repositories.tasks import IMAGE_PROCESSING_LANE_TYPES, TaskRepository, _validate_lane_capacities
from backend.persistence.repositories.thumbnails import DerivedThumbnailRepository
from backend.persistence.repositories.visual_embeddings import VisualEmbeddingRepository, validate_visual_vector

__all__ = [
    "AgentCallbackRequestRepository",
    "CollectionRepository",
    "DerivedThumbnailRepository",
    "IMAGE_PROCESSING_LANE_TYPES",
    "InMemoryAgentCallbackRequestRepository",
    "InMemoryCallbackRequest",
    "InMemoryCallbackRequestRepository",
    "MemeRepository",
    "ReverseImageUsageRepository",
    "SearchRepository",
    "TaskRepository",
    "VisualEmbeddingRepository",
    "_validate_lane_capacities",
    "validate_visual_vector",
]
