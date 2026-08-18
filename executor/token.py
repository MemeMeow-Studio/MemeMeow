"""Compose Agent executor 的内部 token 文件管理。

该模块位于 executor 与 API 配置之间，使用 named volume 持久化内部认证凭据。
首次启动只由非 root executor 生成随机 token；API 只读同一文件，不把凭据写入
仓库、宿主环境或日志。
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path


TOKEN_BYTES = 32
TOKEN_FILE_MODE = 0o600
TOKEN_DIRECTORY_MODE = 0o700
MIN_TOKEN_LENGTH = 32


class ExecutorTokenError(ValueError):
    """内部 token 文件不存在、不安全或内容无效时抛出的稳定错误。"""


def _validate_token_file(path: Path) -> str:
    """以受限权限读取已有 token 文件并返回内存中的 token。

    关键输入是 executor/API 共享的 token 文件路径，输出是去除末尾换行的非空
    token。调用场景是 API 启动读取凭据和 executor 启动复用已有凭据。
    """
    resolved = Path(path).expanduser()
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
    except FileNotFoundError as exc:
        raise ExecutorTokenError("executor_token_file_missing") from exc
    except (OSError, ValueError) as exc:
        raise ExecutorTokenError("executor_token_file_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ExecutorTokenError("executor_token_file_not_regular")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ExecutorTokenError("executor_token_file_permissions_invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            token = handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not token or len(token) < MIN_TOKEN_LENGTH or any(character.isspace() for character in token):
        raise ExecutorTokenError("executor_token_file_invalid")
    return token


def read_token_file(path: str | Path) -> str:
    """读取权限受限的 executor token 文件。

    关键输入是文件路径，输出是供 HTTP Authorization 使用的 token；调用方不会
    获得文件内容以外的诊断细节，错误消息也不包含路径或秘密。
    """
    return _validate_token_file(Path(path))


def ensure_token_file(path: str | Path) -> str:
    """原子创建并读取随机 executor token。

    首次启动在 named volume 中创建 256 位随机 token，文件权限固定为 0600；并发
    启动时只有成功取得 O_EXCL 的进程写入，其他进程复用已完成的文件。
    """
    target = Path(path).expanduser()
    parent = target.parent
    try:
        parent.mkdir(mode=TOKEN_DIRECTORY_MODE, parents=True, exist_ok=True)
        if parent.is_symlink():
            raise ExecutorTokenError("executor_token_directory_symlink_forbidden")
        current_mode = stat.S_IMODE(parent.stat().st_mode)
        if current_mode & 0o077:
            raise ExecutorTokenError("executor_token_directory_permissions_invalid")
    except ExecutorTokenError:
        raise
    except OSError as exc:
        raise ExecutorTokenError("executor_token_directory_unavailable") from exc

    try:
        return _validate_token_file(target)
    except ExecutorTokenError as exc:
        if str(exc) != "executor_token_file_missing":
            raise

    token = secrets.token_urlsafe(TOKEN_BYTES)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, TOKEN_FILE_MODE)
    except FileExistsError:
        return _validate_token_file(target)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return _validate_token_file(target)
        raise ExecutorTokenError("executor_token_file_unwritable") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(token)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), TOKEN_FILE_MODE)
    except OSError as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExecutorTokenError("executor_token_file_unwritable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validate_token_file(target)
