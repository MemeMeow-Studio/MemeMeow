"""PostgreSQL 应用服务的 canonical 模块边界。

metadata、search、tasks 和 worker_manager 分别承载 scope-bound 应用编排；
backend.pg_services 仅保留历史兼容导出。
"""

from .metadata import PostgresMetadataService
from .search import PostgresSearchService
from .tasks import PostgresTaskService
from .worker_manager import PostgresTaskWorkerManager
from .thumbnails import DerivedThumbnailService, ThumbnailError

__all__ = [
    "PostgresMetadataService",
    "PostgresSearchService",
    "PostgresTaskService",
    "PostgresTaskWorkerManager",
    "DerivedThumbnailService",
    "ThumbnailError",
]
