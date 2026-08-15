"""合集 ZIP 资源包格式、导出归档和导入预检服务。

该模块位于合集 API 与 PostgreSQL/BlobStore 之间，只处理可移植的 ZIP 字节和
manifest 规则；业务写入仍由调用方通过现有 scope-bound 存储协调器完成。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from backend.database import BlobStore, DatabaseError
from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key


PACKAGE_FORMAT = "mememeow-collection"
PACKAGE_VERSION = 1
MAX_MEMBERS = 500
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024
MANIFEST_NAME = "manifest.json"
IMAGE_ROOT = "images"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF"}


class CollectionPackageError(RuntimeError):
    """合集包边界错误，携带不会泄露文件系统细节的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class CollectionManifestCollection(BaseModel):
    """manifest 中的合集公开名称。"""

    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=1, max_length=100)


class CollectionManifestMember(BaseModel):
    """manifest 中一张图片的来源身份、文件名和内容指纹。"""

    model_config = ConfigDict(extra="forbid")
    source_meme_id: StrictStr = Field(min_length=1, max_length=128)
    filename_at_export: StrictStr = Field(min_length=1, max_length=255)
    path: StrictStr = Field(min_length=1, max_length=512)
    extension: StrictStr = Field(min_length=2, max_length=16)
    size_bytes: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(min_length=64, max_length=64)


class CollectionManifest(BaseModel):
    """mememeow-collection v1 的严格 manifest 模型。"""

    model_config = ConfigDict(extra="forbid")
    format: StrictStr
    format_version: StrictInt
    collection: CollectionManifestCollection
    members: list[CollectionManifestMember] = Field(default_factory=list, max_length=MAX_MEMBERS)


@dataclass(frozen=True)
class ValidatedPackageMember:
    """预检通过的一张包内图片及其 manifest 记录。"""

    manifest: CollectionManifestMember
    content: bytes


@dataclass(frozen=True)
class ValidatedCollectionPackage:
    """预检通过且尚未写入业务数据库的完整合集包。"""

    manifest: CollectionManifest
    members: tuple[ValidatedPackageMember, ...]


@dataclass(frozen=True)
class ImportTarget:
    """导入成员解析出的目标文件名及可复用现有 Meme。"""

    filename: str
    existing_meme: Any | None = None


def _package_error(code: str, message: str | None = None) -> CollectionPackageError:
    """创建统一的合集包错误，避免把 zipfile/Pillow 原始异常暴露给客户端。"""
    return CollectionPackageError(code, message)


def normalize_collection_name(name: str) -> str:
    """规范合集名称并复用合集 API 的 1 至 100 字符边界。"""
    normalized = unicodedata.normalize("NFC", name).strip() if isinstance(name, str) else ""
    if not 1 <= len(normalized) <= 100:
        raise _package_error("invalid_collection_name")
    if any(ord(character) < 32 for character in normalized):
        raise _package_error("invalid_collection_name")
    return normalized


def normalize_package_filename(filename: str, extension: str | None = None) -> str:
    """校验 manifest 文件名为扁平业务文件名并保留 Unicode 字符。"""
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        raise _package_error("invalid_filename")
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise _package_error("invalid_filename")
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise _package_error("invalid_filename")
    if len(filename.encode("utf-8")) > 255:
        raise _package_error("invalid_filename")
    try:
        validate_business_storage_key(filename)
    except ValueError as exc:
        raise _package_error(str(exc)) from exc
    if extension is not None and Path(filename).suffix.lower() != extension.lower():
        raise _package_error("extension_mismatch")
    return filename


def sha256_bytes(content: bytes) -> str:
    """计算图片字节的 SHA-256，用于 manifest 和导入冲突判断。"""
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """分块计算受控文件 SHA-256，供导出指纹复核使用。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _package_error("member_unreadable") from exc
    return digest.hexdigest()


def serialize_manifest(manifest: CollectionManifest) -> bytes:
    """将严格 manifest 序列化为 UTF-8、稳定键序的 JSON 字节。"""
    try:
        return json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _package_error("manifest_invalid") from exc


def parse_manifest(content: bytes) -> CollectionManifest:
    """解析并校验 manifest JSON，不对业务数据库产生副作用。"""
    try:
        value = json.loads(content.decode("utf-8"))
        manifest = CollectionManifest.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise _package_error("manifest_invalid") from exc
    if manifest.format != PACKAGE_FORMAT or manifest.format_version != PACKAGE_VERSION:
        raise _package_error("unsupported_package_version")
    normalize_collection_name(manifest.collection.name)
    return manifest


def _validate_zip_entry_name(name: str) -> None:
    """校验 ZIP 条目为规范相对 POSIX 路径，阻止穿越和平台路径混淆。"""
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise _package_error("invalid_zip_path")
    path = PurePosixPath(name)
    parts = name.split("/")
    if path.as_posix() != name or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise _package_error("invalid_zip_path")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """检查 ZIP Unix 外部属性中的符号链接类型。"""
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _validate_image_bytes(content: bytes, extension: str) -> None:
    """使用 Pillow 验证图片实际格式与声明扩展名一致且可解码。"""
    expected_format = _IMAGE_FORMATS.get(extension.lower())
    if expected_format is None:
        raise _package_error("unsupported_format")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != expected_format:
                raise _package_error("invalid_image")
            image.verify()
    except CollectionPackageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _package_error("invalid_image") from exc


def preflight_archive(content: bytes, *, max_file_size: int = DEFAULT_MAX_FILE_SIZE, max_total_size: int = MAX_TOTAL_UNCOMPRESSED_BYTES) -> ValidatedCollectionPackage:
    """完整读取并验证 ZIP 中央目录、manifest、图片指纹和解码状态。

    该函数只在内存中读取上传内容；只有返回成功后调用方才允许创建合集或写入图片。
    """
    if not isinstance(content, (bytes, bytearray)):
        raise _package_error("invalid_package")
    try:
        archive = zipfile.ZipFile(BytesIO(bytes(content)))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise _package_error("invalid_zip") from exc
    with archive:
        infos = archive.infolist()
        names: list[str] = []
        total_uncompressed = 0
        for info in infos:
            _validate_zip_entry_name(info.filename)
            if info.is_dir() or _is_symlink(info):
                raise _package_error("unsafe_zip_entry")
            if info.filename in names:
                raise _package_error("duplicate_zip_entry")
            names.append(info.filename)
            if info.file_size < 0 or info.file_size > max_total_size:
                raise _package_error("package_too_large")
            total_uncompressed += info.file_size
            if total_uncompressed > max_total_size:
                raise _package_error("package_too_large")
        if MANIFEST_NAME not in names:
            raise _package_error("manifest_missing")
        if len(names) > MAX_MEMBERS + 1:
            raise _package_error("member_count_exceeded")
        manifest_info = archive.getinfo(MANIFEST_NAME)
        if manifest_info.file_size > 1024 * 1024:
            raise _package_error("manifest_too_large")
        try:
            manifest = parse_manifest(archive.read(manifest_info))
        except (zipfile.BadZipFile, OSError) as exc:
            raise _package_error("invalid_zip") from exc
        if len(manifest.members) > MAX_MEMBERS:
            raise _package_error("member_count_exceeded")
        total_declared = 0
        expected_paths: set[str] = set()
        source_ids: set[str] = set()
        for member in manifest.members:
            if member.source_meme_id in source_ids:
                raise _package_error("duplicate_member")
            source_ids.add(member.source_meme_id)
            if any(ord(character) < 32 for character in member.source_meme_id) or "/" in member.source_meme_id or "\\" in member.source_meme_id:
                raise _package_error("invalid_source_meme_id")
            extension = member.extension.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise _package_error("unsupported_format")
            if member.extension != extension:
                raise _package_error("extension_mismatch")
            normalize_package_filename(member.filename_at_export, extension)
            _validate_zip_entry_name(member.path)
            path = PurePosixPath(member.path)
            if len(path.parts) != 2 or path.parts[0] != IMAGE_ROOT or path.parts[1] != path.name:
                raise _package_error("invalid_zip_path")
            # v1 路径由来源 ID 和扩展名确定，拒绝把任意条目伪装成另一张成员图片。
            expected_path = f"{IMAGE_ROOT}/{member.source_meme_id}{extension}"
            if member.path != expected_path:
                raise _package_error("invalid_zip_path")
            if Path(member.path).suffix.lower() != extension or member.path in expected_paths:
                raise _package_error("extension_mismatch" if member.path not in expected_paths else "duplicate_member")
            expected_paths.add(member.path)
            if not _SHA256_RE.fullmatch(member.sha256):
                raise _package_error("invalid_sha256")
            if member.size_bytes > max_file_size:
                raise _package_error("file_too_large")
            total_declared += member.size_bytes
            if total_declared > max_total_size:
                raise _package_error("package_too_large")
        actual_paths = set(names) - {MANIFEST_NAME}
        if actual_paths != expected_paths:
            raise _package_error("manifest_entries_mismatch")
        validated: list[ValidatedPackageMember] = []
        for member in manifest.members:
            info = archive.getinfo(member.path)
            if info.file_size != member.size_bytes:
                raise _package_error("size_mismatch")
            try:
                image = archive.read(info)
            except (zipfile.BadZipFile, OSError) as exc:
                raise _package_error("invalid_zip") from exc
            if len(image) != member.size_bytes:
                raise _package_error("size_mismatch")
            if sha256_bytes(image) != member.sha256:
                raise _package_error("sha256_mismatch")
            _validate_image_bytes(image, member.extension)
            validated.append(ValidatedPackageMember(manifest=member, content=image))
        return ValidatedCollectionPackage(manifest=manifest, members=tuple(validated))


def resolve_import_filename(filename: str, sha256: str, existing: Mapping[str, Any]) -> ImportTarget:
    """按同名同 SHA 复用、同名异 SHA 哈希后缀规则解析导入文件名。"""
    clean = normalize_package_filename(filename)
    current = existing.get(clean)
    if current is None:
        return ImportTarget(clean)
    current_meme = current.get("meme") if isinstance(current, Mapping) else current
    current_sha = current.get("sha256") if isinstance(current, Mapping) else getattr(current, "sha256", None)
    if str(current_sha) == sha256:
        return ImportTarget(clean, current_meme)
    stem = Path(clean).stem
    suffix = Path(clean).suffix.lower()
    for prefix_length in range(8, 65, 8):
        candidate = f"{stem}-{sha256[:prefix_length]}{suffix}"
        try:
            normalize_package_filename(candidate, suffix)
        except CollectionPackageError:
            continue
        other = existing.get(candidate)
        if other is None:
            return ImportTarget(candidate)
        other_meme = other.get("meme") if isinstance(other, Mapping) else other
        other_sha = other.get("sha256") if isinstance(other, Mapping) else getattr(other, "sha256", None)
        if str(other_sha) == sha256:
            return ImportTarget(candidate, other_meme)
    raise _package_error("filename_conflict")


def _read_export_member(member: Any, blob_store: BlobStore, *, max_file_size: int, max_total_size: int, total_size: int) -> tuple[bytes, int]:
    """读取并复核单张导出成员，避免生成不完整归档。"""
    try:
        image = blob_store.resolve(member.storage_key)
        stat_result = image.stat()
        if stat_result.st_size != member.size_bytes or stat_result.st_size > max_file_size:
            raise _package_error("member_changed")
        content = image.read_bytes()
    except CollectionPackageError:
        raise
    except (DatabaseError, OSError) as exc:
        raise _package_error("member_unreadable") from exc
    if len(content) != member.size_bytes or sha256_bytes(content) != member.sha256:
        raise _package_error("member_changed")
    if total_size + len(content) > max_total_size:
        raise _package_error("package_too_large")
    return content, total_size + len(content)


def build_export_archive(collection_name: str, members: Sequence[Any], blob_store: BlobStore, *, temp_root: Path, max_file_size: int = DEFAULT_MAX_FILE_SIZE, max_total_size: int = MAX_TOTAL_UNCOMPRESSED_BYTES) -> Path:
    """现场读取合集成员并先完整写入临时 ZIP，成功前绝不返回归档路径。"""
    if len(members) > MAX_MEMBERS:
        raise _package_error("member_count_exceeded")
    temp_root = temp_root.expanduser().resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="collection-export-", suffix=".zip", dir=temp_root, delete=False)
    archive_path = Path(handle.name)
    handle.close()
    total_size = 0
    manifest_members: list[CollectionManifestMember] = []
    image_entries: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED) as archive:
            for member in members:
                content, total_size = _read_export_member(member, blob_store, max_file_size=max_file_size, max_total_size=max_total_size, total_size=total_size)
                extension = str(member.extension).lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    raise _package_error("unsupported_format")
                path = f"{IMAGE_ROOT}/{member.id}{extension}"
                filename = normalize_package_filename(str(member.storage_key), extension)
                manifest_members.append(CollectionManifestMember(source_meme_id=str(member.id), filename_at_export=filename, path=path, extension=extension, size_bytes=len(content), sha256=sha256_bytes(content)))
                image_entries.append((path, content))
            manifest = CollectionManifest(format=PACKAGE_FORMAT, format_version=PACKAGE_VERSION, collection=CollectionManifestCollection(name=normalize_collection_name(collection_name)), members=manifest_members)
            info = zipfile.ZipInfo(MANIFEST_NAME)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, serialize_manifest(manifest))
            for path, content in image_entries:
                info = zipfile.ZipInfo(path)
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o600 << 16
                archive.writestr(info, content)
        return archive_path
    except Exception:
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def safe_download_filename(collection_name: str) -> str:
    """将合集名称转换为不含路径和控制字符的下载文件名。"""
    value = unicodedata.normalize("NFKC", collection_name).strip()
    value = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", "_", value).strip(" .") or "collection"
    encoded = value.encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip(" .") or "collection"
    return f"{encoded}.zip"


def cleanup_archive(path: Path) -> None:
    """清理导出临时文件，响应完成或异常时均可安全调用。"""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if path.parent.name.startswith(".collection-export-"):
        shutil.rmtree(path.parent, ignore_errors=True)
