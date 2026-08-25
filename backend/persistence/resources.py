"""scope-bound 数据环境与进程级数据库资源装配。

该模块位于事务单元和既有 Repository/文件存储实现之间。它负责为请求或任务创建
共享 Session 的 repository 组合，并维护 Engine、Session 工厂和按 scope 创建的
BlobStore 生命周期。Repository、BlobStore 与 StorageCoordinator 仍以原实现为准，
只在调用点延迟导入以避免兼容 facade 的循环依赖。
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.persistence.engine import DatabaseError, SCOPE_LOCAL, ensure_optional_control_schema
from backend.persistence.models import Scope, ScopeContext
from backend.persistence.unit_of_work import UnitOfWork


class DataEnvironment:
    """请求或任务级 scope-bound Session、repository 和 BlobStore 组合。"""

    def __init__(self, factory: sessionmaker[Session], scope: ScopeContext):
        """创建共享一个 Session 的 scope-bound repository 环境。

        参数 `factory` 提供请求级 Session，`scope` 固定当前业务范围；Repository 只
        通过该 scope 读取或写入结构化数据，环境退出时由 UnitOfWork 结束事务。
        """
        from backend.database import (
            AgentCallbackRequestRepository,
            CollectionRepository,
            MemeRepository,
            ReverseImageUsageRepository,
            SearchRepository,
            TaskRepository,
            VisualEmbeddingRepository,
        )

        self.uow = UnitOfWork(factory, scope)
        self.scope = scope
        self.memes = MemeRepository(self.uow.session, scope)
        self.collections = CollectionRepository(self.uow.session, scope)
        self.search = SearchRepository(self.uow.session, scope)
        self.visual = VisualEmbeddingRepository(self.uow.session, scope)
        # 兼容领域层较直白的命名，调用方不得绕过 scope-bound repository。
        self.visual_embeddings = self.visual
        self.tasks = TaskRepository(self.uow.session, scope)
        self.reverse_image_usage = ReverseImageUsageRepository(self.uow.session, scope)
        self.callback_requests = AgentCallbackRequestRepository(self.uow.session, scope)

    def __enter__(self) -> "DataEnvironment":
        """进入共享事务并返回当前环境。"""
        self.uow.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """将退出结果交给 UnitOfWork，提交或回滚并关闭共享 Session。"""
        self.uow.__exit__(exc_type, exc, tb)


class DatabaseResources:
    """生命周期共享 Engine、Session 工厂和按 scope 创建的 BlobStore。"""

    def __init__(self, engine: Engine, *, image_root: Path, data_root: Path, settings: Any | None = None, require_local_scope: bool = True):
        """初始化进程级数据库资源并校验 local scope 安装状态。

        `engine`、`image_root` 和 `data_root` 分别提供共享连接池、local 图片根目录和
        非 local scope 的数据根目录；`require_local_scope` 控制宿主是否必须已安装 local
        scope。构造期间会幂等补齐可选控制面表，但不会推进 migration revision。
        """
        from backend.database import BlobStore

        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False, class_=Session)
        self.image_root = image_root
        self.data_root = data_root
        self._scope_cache: dict[str, Scope] = {}
        self._lock = Lock()
        ensure_optional_control_schema(engine)
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == SCOPE_LOCAL))
            if scope is None and require_local_scope:
                raise DatabaseError("installation_required")
            if scope is not None:
                self._scope_cache[SCOPE_LOCAL] = scope
            namespace = scope.storage_namespace if scope else None
        # 适配宿主未必部署 local scope；此时保留 None，任何误用 local 都会稳定失败。
        self.blob_store = BlobStore(root=image_root, scope=ScopeContext(SCOPE_LOCAL), storage_namespace=namespace, local=True) if scope is not None or require_local_scope else None

    def environment(self, scope_id: str | ScopeContext | None = None) -> DataEnvironment:
        """创建指定 scope 的请求级环境；缺失 scope 时 fail-closed。"""
        if scope_id is None:
            raise DatabaseError("scope_required")
        context = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        return DataEnvironment(self.factory, context)

    def flat_preflight(self, scope_id: str | ScopeContext | None = None) -> dict[str, Any]:
        """执行指定 scope 的扁平图片库只读预检；缺失 scope 时 fail-closed。"""
        from backend.database import StorageCoordinator

        if scope_id is None:
            raise DatabaseError("scope_required")
        return StorageCoordinator(self, scope_id=scope_id).flat_preflight()

    def blob_store_for_scope(self, scope_id: str | ScopeContext) -> BlobStore:
        """读取 scope 的不可变 storage_namespace 并创建绑定 BlobStore。"""
        from backend.database import BlobStore

        context = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        scope_id = context.scope_id
        if scope_id == SCOPE_LOCAL:
            if self.blob_store is None:
                raise DatabaseError("scope_not_found")
            return self.blob_store
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == scope_id))
            if scope is None:
                raise DatabaseError("scope_not_found")
            return BlobStore(root=self.data_root, scope=ScopeContext(scope_id), storage_namespace=scope.storage_namespace, local=False)
