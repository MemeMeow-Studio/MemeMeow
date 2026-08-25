"""同步数据库事务单元。

该模块提供请求或任务级的 Session 生命周期边界。它只依赖模型层的 scope context，
不依赖兼容 facade、Repository 或文件存储，供 resources 模块装配现有 repository。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from backend.persistence.models import ScopeContext


class UnitOfWork:
    """同步事务边界；成功退出提交，异常退出回滚并关闭 Session。"""

    def __init__(self, factory: sessionmaker[Session], scope: ScopeContext):
        """创建绑定 scope 的 Session。

        参数 `factory` 是进程级 Session 工厂，`scope` 是当前请求或任务的不可变范围；
        新建的 Session 由上下文管理器负责提交、回滚和关闭。
        """
        if not isinstance(scope, ScopeContext):
            raise ValueError("scope_required")
        self.scope = scope
        self.session = factory()

    def __enter__(self) -> "UnitOfWork":
        """进入事务上下文并返回当前 UnitOfWork。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """按上下文是否异常提交或回滚，并始终关闭 Session。"""
        try:
            if exc_type:
                self.session.rollback()
            else:
                self.session.commit()
        finally:
            self.session.close()

    def rollback(self) -> None:
        """显式回滚当前事务，供跨存储补偿路径使用。"""
        self.session.rollback()
