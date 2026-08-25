"""PostgreSQL 应用服务的历史兼容 facade。

具体实现按 metadata、search、task 和 worker-manager 职责位于
``backend.services``；本模块只显式 re-export 同一组 canonical class，供 API、scope
工厂、脚本、图片处理 Worker 和旧测试继续使用原导入路径。
"""

import logging

from backend.services.metadata import PostgresMetadataService
from backend.services.search import PostgresSearchService
from backend.services.tasks import PostgresTaskService
from backend.services.worker_manager import PostgresTaskWorkerManager

# 保留旧模块级 logger 对象，兼容宿主对 backend.pg_services 的日志配置和测试替身。
logger = logging.getLogger("backend.pg_services")

__all__ = [
    "PostgresMetadataService",
    "PostgresSearchService",
    "PostgresTaskService",
    "PostgresTaskWorkerManager",
]
