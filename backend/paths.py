"""受控图片标识解析器，阻止路径穿越和符号链接逃逸。"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException


SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


class PathResolver:
    """将客户端相对标识解析到图片根目录内的真实文件。"""

    def __init__(self, image_root: Path):
        self.root = image_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_directory(self, directory: str = "") -> Path:
        """解析目录标识并校验其位于根目录内且不是符号链接逃逸。"""
        if not isinstance(directory, str) or directory.startswith(("/", "\\")) or "\x00" in directory:
            raise HTTPException(400, detail={"error": "invalid_path", "message": "目录标识非法"})
        candidate = (self.root / directory).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "路径超出图片根目录"})
        if not candidate.exists() or not candidate.is_dir():
            raise HTTPException(404, detail={"error": "directory_not_found", "message": "图片目录不存在"})
        return candidate

    def resolve_file(self, directory: str, filename: str, *, must_exist: bool = True) -> Path:
        """解析图片文件并校验扩展名、根目录边界与符号链接。"""
        if not SAFE_NAME.fullmatch(filename or ""):
            raise HTTPException(400, detail={"error": "invalid_filename", "message": "文件名非法"})
        if Path(filename).name != filename or Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(400, detail={"error": "invalid_filename", "message": "仅支持 PNG/JPG/JPEG/GIF"})
        parent = self.resolve_directory(directory)
        candidate = (parent / filename).resolve()
        if self.root not in candidate.parents or candidate == self.root:
            raise HTTPException(403, detail={"error": "path_forbidden", "message": "路径超出图片根目录"})
        if must_exist and (not candidate.exists() or not candidate.is_file()):
            raise HTTPException(404, detail={"error": "file_not_found", "message": "图片不存在"})
        return candidate

    def relative(self, path: Path) -> str:
        """返回统一的 POSIX 相对标识，不暴露服务器绝对路径。"""
        return path.resolve().relative_to(self.root).as_posix()

    def media_url(self, path: Path) -> str:
        """生成受控媒体接口 URL。"""
        relative = self.relative(path)
        parent = Path(relative).parent.as_posix()
        directory = "" if parent == "." else parent
        return f"/media/{relative}" if directory else f"/media/{Path(relative).name}"
