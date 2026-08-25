"""scope 绑定的持久化 Repository 子包。

图片记录、合集关系和文本检索分别由独立模块实现；backend.database 继续提供历史兼容导出。
"""

from backend.persistence.repositories.collections import CollectionRepository
from backend.persistence.repositories.memes import MemeRepository
from backend.persistence.repositories.search import SearchRepository

__all__ = ["CollectionRepository", "MemeRepository", "SearchRepository"]
