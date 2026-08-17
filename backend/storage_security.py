"""受控文件根目录的启动期安全校验。

该模块位于配置和 BlobStore 之间，确保业务进程不会把符号链接或无权限目录
当作图片、runtime 等受控存储根使用。具体的历史文件所有权迁移由 Compose
初始化服务完成，本模块只验证当前进程是否能够安全访问目录。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class StorageRootError(ValueError):
    """受控存储根不存在、不安全或当前身份无法访问时抛出的稳定错误。"""


def _absolute_path(path: str | Path) -> Path:
    """把路径转为不解析符号链接的绝对路径，保留后续 lstat 检查语义。"""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else Path(os.path.abspath(candidate))


def _reject_symlink_ancestors(path: Path) -> None:
    """逐级检查已经存在的路径组件，防止根路径通过父级链接越界。"""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # 缺失组件之后不可能存在后续组件；创建时由 mkdir(parents=True) 补齐。
            break
        except OSError as exc:
            raise StorageRootError("storage_root_unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageRootError("storage_root_symlink_forbidden")


def validate_controlled_root(
    path: str | Path,
    *,
    create: bool = True,
    writable: bool = True,
) -> Path:
    """校验并返回受控目录。

    关键输入是图片或 runtime 根路径，输出是不解析链接的绝对目录。调用场景是
    API 启动和 BlobStore 构造；目录缺失时可创建，但任何符号链接、特殊节点或
    当前身份缺少读写执行权限都会以稳定错误终止启动。
    """
    candidate = _absolute_path(path)
    _reject_symlink_ancestors(candidate)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if not create:
            raise StorageRootError("storage_root_missing")
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = candidate.lstat()
        except OSError as exc:
            raise StorageRootError("storage_root_unavailable") from exc
    except OSError as exc:
        raise StorageRootError("storage_root_unreadable") from exc

    if stat.S_ISLNK(metadata.st_mode):
        raise StorageRootError("storage_root_symlink_forbidden")
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageRootError("storage_root_not_directory")
    if writable:
        access_mode = os.R_OK | os.W_OK | os.X_OK
    else:
        access_mode = os.R_OK | os.X_OK
    if not os.access(candidate, access_mode):
        raise StorageRootError("storage_root_access_denied")
    return candidate


__all__ = ["StorageRootError", "validate_controlled_root"]
