"""Compose 运行时受控存储初始化程序。

该模块只由短生命周期的 ``runtime-init`` 服务以 root 身份运行，处理显式挂载的
图片根、Agent runtime volume 和 executor token volume。它不连接网络、数据库或
Docker socket，只通过 chown/chmod 归一化受控目录中的节点。
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable

from backend.storage_security import StorageRootError, validate_controlled_root


RUNTIME_DIRECTORY_MODE = 0o700
RUNTIME_FILE_MODE = 0o600
RUNTIME_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")


class RuntimeInitError(ValueError):
    """受控存储初始化失败时返回给部署入口的稳定错误。"""


def parse_runtime_identity(uid: str | int | None, gid: str | int | None) -> tuple[int, int]:
    """解析并校验目标运行身份。

    关键输入是 Compose 注入的 UID/GID，输出是非 root 数值身份。调用场景是
    初始化服务启动前；缺失、非数字、非正数或任一 root 身份都会被拒绝。
    """
    values: list[int] = []
    for value, name in ((uid, "uid"), (gid, "gid")):
        if isinstance(value, bool) or value is None:
            raise RuntimeInitError(f"runtime_{name}_invalid")
        text = str(value)
        if not RUNTIME_ID_PATTERN.fullmatch(text):
            raise RuntimeInitError(f"runtime_{name}_invalid")
        try:
            parsed = int(text, 10)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeInitError(f"runtime_{name}_invalid") from exc
        if parsed <= 0:
            raise RuntimeInitError(f"runtime_{name}_invalid")
        values.append(parsed)
    return values[0], values[1]


def _reject_unsafe_node(path: Path, metadata: os.stat_result, *, root: bool = False) -> None:
    """拒绝符号链接、特殊节点和不安全的多链接普通文件。"""
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        raise RuntimeInitError("runtime_storage_symlink_forbidden")
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise RuntimeInitError("runtime_storage_special_node_forbidden")
    if stat.S_ISREG(mode) and metadata.st_nlink != 1:
        raise RuntimeInitError("runtime_storage_hardlink_forbidden")
    if root and not stat.S_ISDIR(mode):
        raise RuntimeInitError("runtime_storage_root_not_directory")


def _scan_tree(root: Path) -> None:
    """在修改任何节点前完整扫描一棵显式受控树。"""
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise RuntimeInitError("runtime_storage_unreadable") from exc
    _reject_unsafe_node(root, metadata, root=True)
    if not stat.S_ISDIR(metadata.st_mode):
        return
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise RuntimeInitError("runtime_storage_unreadable") from exc
    for entry in entries:
        child = Path(entry.path)
        try:
            child_metadata = child.lstat()
        except OSError as exc:
            raise RuntimeInitError("runtime_storage_unreadable") from exc
        _reject_unsafe_node(child, child_metadata)
        if stat.S_ISDIR(child_metadata.st_mode):
            _scan_tree(child)


def _set_metadata(path: Path, uid: int, gid: int, mode: int) -> None:
    """以不跟随链接的系统调用设置单个受控节点的身份和权限。"""
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
    except (OSError, TypeError, NotImplementedError) as exc:
        raise RuntimeInitError("runtime_storage_metadata_update_failed") from exc


def _normalize_tree(root: Path, uid: int, gid: int) -> None:
    """递归把目录设为 0700、普通文件设为 0600。"""
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise RuntimeInitError("runtime_storage_unreadable") from exc
    _reject_unsafe_node(root, metadata)
    if stat.S_ISDIR(metadata.st_mode):
        try:
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimeInitError("runtime_storage_unreadable") from exc
        for entry in entries:
            _normalize_tree(Path(entry.path), uid, gid)
        _set_metadata(root, uid, gid, RUNTIME_DIRECTORY_MODE)
    else:
        _set_metadata(root, uid, gid, RUNTIME_FILE_MODE)


def _ensure_directory(path: Path) -> None:
    """在受控根内创建一个目录，并立即检查其节点类型。"""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=RUNTIME_DIRECTORY_MODE, parents=True, exist_ok=True)
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeInitError("runtime_storage_directory_create_failed") from exc
    except OSError as exc:
        raise RuntimeInitError("runtime_storage_unreadable") from exc
    _reject_unsafe_node(path, metadata, root=True)


def _prepare_roots(paths: Iterable[Path]) -> tuple[Path, ...]:
    """校验三个显式挂载根并返回不解析链接的绝对路径。"""
    prepared: list[Path] = []
    for path in paths:
        try:
            prepared.append(validate_controlled_root(path, create=True, writable=True))
        except StorageRootError as exc:
            raise RuntimeInitError(str(exc)) from exc
    return tuple(prepared)


def initialize_storage(
    image_root: str | Path,
    runtime_root: str | Path,
    token_root: str | Path,
    uid: str | int,
    gid: str | int,
) -> tuple[int, int]:
    """初始化图片、runtime 与 token 三个受控根并返回目标身份。

    该函数先完整扫描现有节点，再创建固定业务目录并归一化所有权和模式；因此
    发现链接、特殊节点或硬链接时不会先改写已存在图片，图片字节始终保持不变。
    """
    target_uid, target_gid = parse_runtime_identity(uid, gid)
    roots = _prepare_roots((Path(image_root), Path(runtime_root), Path(token_root)))

    # 先拒绝全部危险节点，再开始修改权限，避免部分迁移掩盖存储完整性错误。
    for root in roots:
        _scan_tree(root)

    image_root, runtime_root, token_root = roots
    for path in (
        image_root / ".staging",
        image_root / ".quarantine",
        runtime_root / "home",
        runtime_root / "workspace",
        runtime_root / "workspace" / ".opencode",
        runtime_root / "workspace" / ".opencode" / "skills",
        runtime_root / "task-results",
        runtime_root / "logs",
        runtime_root / "slots",
        runtime_root / "reverse_image_cache",
        runtime_root / "reverse_image_cache" / "serpapi_google_lens",
    ):
        _ensure_directory(path)

    # 固定目录创建后再次扫描，覆盖并发写入和新建节点的类型检查。
    for root in roots:
        _scan_tree(root)
    for root in roots:
        _normalize_tree(root, target_uid, target_gid)
    return target_uid, target_gid


def _parser() -> argparse.ArgumentParser:
    """构造只接受三个受控挂载根的命令行解析器。"""
    parser = argparse.ArgumentParser(description="初始化 MemeMeow 受控运行时存储")
    parser.add_argument("--image-root", default="/images")
    parser.add_argument("--runtime-root", default="/runtime")
    parser.add_argument("--token-root", default="/run/agent-executor-secret")
    parser.add_argument("--uid", default=os.environ.get("MEMEMEOW_RUNTIME_UID"))
    parser.add_argument("--gid", default=os.environ.get("MEMEMEOW_RUNTIME_GID"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行一次初始化并返回适合 Compose 的退出码。"""
    arguments = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("runtime_init_failed:runtime_init_requires_root", file=sys.stderr)
        return 2
    try:
        initialize_storage(
            image_root=arguments.image_root,
            runtime_root=arguments.runtime_root,
            token_root=arguments.token_root,
            uid=arguments.uid,
            gid=arguments.gid,
        )
    except (RuntimeInitError, OSError) as exc:
        code = str(exc) or "runtime_storage_initialization_failed"
        print(f"runtime_init_failed:{code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
