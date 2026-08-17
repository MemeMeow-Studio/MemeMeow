"""扁平图片存储标识解析器，阻止路径穿越和符号链接逃逸。"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException

from backend.storage_security import StorageRootError, validate_controlled_root


SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")
INTERNAL_STORAGE_NAMES = frozenset({".staging", ".quarantine"})


def validate_business_storage_key(value: str) -> str:
    """校验业务图片只能使用当前 scope 根目录下的单个安全文件名。

    该函数供 API、repository、StorageCoordinator 和 migration 共用；BlobStore
    的内部 ``.staging``/``.quarantine`` key 不经过这里校验。
    """
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("invalid_storage_key")
    if not SAFE_NAME.fullmatch(value) or Path(value).name != value:
        raise ValueError("invalid_storage_key")
    if value in INTERNAL_STORAGE_NAMES or value.startswith(tuple(f"{name}." for name in INTERNAL_STORAGE_NAMES)):
        raise ValueError("reserved_storage_key")
    if value.strip(" .") != value:
        raise ValueError("invalid_storage_key")
    if Path(value).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("unsupported_format")
    return value


class PathResolver:
    """将客户端相对标识解析到图片根目录内的真实文件。"""

    def __init__(self, image_root: Path):
        try:
            self.root = validate_controlled_root(image_root, create=True, writable=True)
        except StorageRootError as exc:
            raise ValueError(str(exc)) from exc

    def resolve_file(self, filename: str, *, must_exist: bool = True) -> Path:
        """解析图片文件并校验扩展名、根目录边界与符号链接。"""
        try:
            validate_business_storage_key(filename)
        except ValueError as exc:
            raise HTTPException(400, detail={"error": "invalid_filename", "message": "文件名非法"})
        lexical = self.root / filename
        if lexical.is_symlink():
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "符号链接文件不允许访问"})
        candidate = lexical.resolve()
        if self.root not in candidate.parents or candidate == self.root:
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "路径超出图片根目录"})
        if must_exist and (not candidate.exists() or not candidate.is_file()):
            raise HTTPException(404, detail={"error": "file_not_found", "message": "图片不存在"})
        return candidate

    def relative(self, path: Path) -> str:
        """返回统一的 POSIX 相对标识，不暴露服务器绝对路径。"""
        if path == self.root or path.resolve() == self.root:
            return ""
        if path.is_symlink():
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "符号链接文件不允许访问"})
        resolved = path.resolve()
        if resolved == self.root or self.root not in resolved.parents:
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "路径超出图片根目录"})
        return resolved.relative_to(self.root).as_posix()

    def media_url(self, path: Path) -> str:
        """生成受控媒体接口 URL。"""
        relative = self.relative(path)
        return f"/media/{Path(relative).name}"
