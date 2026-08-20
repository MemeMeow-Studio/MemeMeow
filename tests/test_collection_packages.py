"""合集 ZIP v1 格式、导出完整性和导入安全预检单元测试。"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from backend.collection_packages import (
    CollectionManifest,
    CollectionManifestCollection,
    CollectionManifestMember,
    CollectionPackageError,
    MAX_ARCHIVE_COMPRESSED_BYTES,
    build_export_archive,
    cleanup_archive,
    parse_manifest,
    preflight_archive,
    resolve_import_filename,
    sha256_bytes,
    serialize_manifest,
)
from backend.database import BlobStore, ScopeContext


def png_bytes(color: str = "red") -> bytes:
    """生成可解码的最小 PNG，供资源包单元测试复用。"""
    output = io.BytesIO()
    Image.new("RGB", (3, 3), color=color).save(output, format="PNG")
    return output.getvalue()


def package_bytes(manifest: CollectionManifest, images: dict[str, bytes]) -> bytes:
    """按照资源包根目录规则构造内存 ZIP。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        for path, content in images.items():
            archive.writestr(path, content)
    return output.getvalue()


def compressed_package_bytes(manifest: CollectionManifest, images: dict[str, bytes]) -> bytes:
    """使用 DEFLATE 构造可触发压缩放大比检查的 ZIP。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        for path, content in images.items():
            archive.writestr(path, content)
    return output.getvalue()


def member(*, source_id: str, filename: str, content: bytes) -> CollectionManifestMember:
    """创建与给定图片内容一致的 manifest 成员。"""
    extension = Path(filename).suffix.lower()
    return CollectionManifestMember(source_meme_id=source_id, filename_at_export=filename, path=f"images/{source_id}{extension}", extension=extension, size_bytes=len(content), sha256=__import__("hashlib").sha256(content).hexdigest())


def test_manifest_roundtrip_preserves_unicode_and_empty_collection() -> None:
    """空合集和 Unicode 名称可以稳定序列化并往返。"""
    manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="猫猫 · 工作"), members=[])
    assert parse_manifest(serialize_manifest(manifest)) == manifest
    package = preflight_archive(package_bytes(manifest, {}))
    assert package.manifest.collection.name == "猫猫 · 工作"
    assert package.members == ()


def test_export_writes_manifest_and_cleans_temporary_archive(tmp_path: Path) -> None:
    """导出归档包含可校验 manifest，清理函数删除文件和临时目录。"""
    store = BlobStore(root=tmp_path / "images", scope=ScopeContext("local"), local=True)
    content = png_bytes()
    image = store.root / "疑惑.png"
    image.write_bytes(content)
    meme = SimpleNamespace(id=uuid4(), storage_key="疑惑.png", extension=".png", size_bytes=len(content), sha256=__import__("hashlib").sha256(content).hexdigest())
    export_dir = tmp_path / ".collection-export-test"
    archive_path = build_export_archive("我的合集", [meme], store, temp_root=export_dir)
    package = preflight_archive(archive_path.read_bytes())
    assert package.manifest.collection.name == "我的合集"
    assert package.members[0].manifest.filename_at_export == "疑惑.png"
    assert package.members[0].content == content
    cleanup_archive(archive_path)
    assert not archive_path.exists()
    cleanup_archive(export_dir)


def test_preflight_rejects_path_attacks_duplicate_entries_and_bad_content() -> None:
    """预检拒绝路径穿越、重复条目、哈希错误和不可解码图片。"""
    content = png_bytes()
    valid_member = member(source_id="source-1", filename="safe.png", content=content)
    manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="安全"), members=[valid_member])
    attacked = io.BytesIO()
    with zipfile.ZipFile(attacked, "w") as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        archive.writestr("../outside.png", content)
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(attacked.getvalue())
    assert error.value.code == "invalid_zip_path"

    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        archive.writestr(valid_member.path, content)
        archive.writestr(valid_member.path, content)
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(duplicate.getvalue())
    assert error.value.code == "duplicate_zip_entry"

    bad_member = valid_member.model_copy(update={"sha256": "0" * 64})
    bad_manifest = manifest.model_copy(update={"members": [bad_member]})
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(bad_manifest, {valid_member.path: content}))
    assert error.value.code == "sha256_mismatch"

    mismatched_path = valid_member.model_copy(update={"path": "images/another.png"})
    mismatched_manifest = manifest.model_copy(update={"members": [mismatched_path]})
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(mismatched_manifest, {mismatched_path.path: content}))
    assert error.value.code == "invalid_zip_path"


def test_preflight_rejects_unknown_format_and_resource_limits() -> None:
    """未知格式、单文件和总大小限制在业务写入前失败。"""
    content = png_bytes()
    manifest = CollectionManifest(format="other", format_version=1, collection=CollectionManifestCollection(name="无效"), members=[])
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(manifest, {}))
    assert error.value.code == "unsupported_package_version"

    one = member(source_id="large", filename="large.png", content=content)
    large_manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="限制"), members=[one])
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(large_manifest, {one.path: content}), max_file_size=len(content) - 1)
    assert error.value.code == "file_too_large"

    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(large_manifest, {one.path: content}), max_total_size=len(content) - 1)
    assert error.value.code == "package_too_large"

    archive = package_bytes(large_manifest, {one.path: content})
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        total_size = sum(info.file_size for info in zipped.infolist())
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(archive, max_total_size=total_size - 1)
    assert error.value.code == "package_too_large"


def test_preflight_rejects_archive_size_and_compression_amplification() -> None:
    """压缩包原始字节和单成员放大比在读取图片前受限。"""
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(b"x" * (MAX_ARCHIVE_COMPRESSED_BYTES + 1))
    assert error.value.code == "archive_too_large"

    content = b"0" * (512 * 1024)
    item = member(source_id="bomb", filename="bomb.png", content=content)
    manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="放大"), members=[item])
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(compressed_package_bytes(manifest, {item.path: content}))
    assert error.value.code == "compression_ratio_exceeded"


def test_preflight_rejects_declared_member_count_before_materializing_all_entries() -> None:
    """中央目录声明超过 500 个成员时在构造完整条目校验前拒绝。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(502):
            archive.writestr(f"entry-{index}.bin", b"")
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(output.getvalue())
    assert error.value.code == "member_count_exceeded"


def test_preflight_rejects_zip64_nested_and_special_entries() -> None:
    """ZIP64、嵌套归档和 Unix 特殊文件不能进入图片预检。"""
    content = png_bytes()
    item = member(source_id="zip64", filename="zip64.png", content=content)
    manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="ZIP64"), members=[item])
    zip64_output = io.BytesIO()
    with zipfile.ZipFile(zip64_output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        with archive.open(item.path, "w", force_zip64=True) as handle:
            handle.write(content)
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(zip64_output.getvalue())
    assert error.value.code == "zip64_not_supported"

    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("nested.txt", b"nested")
    nested_item = member(source_id="nested", filename="nested.png", content=nested.getvalue())
    nested_manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="嵌套"), members=[nested_item])
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(package_bytes(nested_manifest, {nested_item.path: nested.getvalue()}))
    assert error.value.code == "nested_archive"

    special_item = member(source_id="fifo", filename="fifo.png", content=content)
    special_manifest = CollectionManifest(format="mememeow-collection", format_version=1, collection=CollectionManifestCollection(name="特殊"), members=[special_item])
    special_output = io.BytesIO()
    with zipfile.ZipFile(special_output, "w") as archive:
        archive.writestr("manifest.json", serialize_manifest(special_manifest))
        info = zipfile.ZipInfo(special_item.path)
        info.create_system = 3
        info.external_attr = stat.S_IFIFO << 16
        archive.writestr(info, content)
    with pytest.raises(CollectionPackageError) as error:
        preflight_archive(special_output.getvalue())
    assert error.value.code == "unsafe_zip_entry"


def test_filename_conflicts_use_sha_prefix_and_reuse_same_content() -> None:
    """同名同 SHA 复用，异 SHA 从八位哈希前缀开始递增。"""
    old = SimpleNamespace(id=uuid4(), sha256="a" * 64)
    digest = "b" * 64
    reused = resolve_import_filename("猫.png", old.sha256, {"猫.png": old})
    assert reused.filename == "猫.png" and reused.existing_meme is old
    first_conflict = f"猫-{digest[:8]}.png"
    resolved = resolve_import_filename("猫.png", digest, {"猫.png": old, first_conflict: SimpleNamespace(id=uuid4(), sha256="c" * 64)})
    assert resolved.filename == f"猫-{digest[:16]}.png"


def test_cleanup_archive_unlinks_symlink_without_touching_target(tmp_path: Path) -> None:
    """清理被替换为符号链接的临时路径时只删除链接本身。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    link = tmp_path / ".collection-export-link"
    link.symlink_to(outside, target_is_directory=True)
    cleanup_archive(link)
    assert not link.exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_export_rejects_member_symlink_and_cleans_archive_directory(tmp_path: Path) -> None:
    """导出成员被替换为符号链接时拒绝读取并清理临时归档目录。"""
    store = BlobStore(root=tmp_path / "images", scope=ScopeContext("local"), local=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes("blue"))
    link = store.root / "link.png"
    link.symlink_to(outside)
    meme = SimpleNamespace(id=uuid4(), storage_key="link.png", extension=".png", size_bytes=outside.stat().st_size, sha256=sha256_bytes(outside.read_bytes()))
    export_dir = tmp_path / ".collection-export-race"
    with pytest.raises(CollectionPackageError) as error:
        build_export_archive("竞态", [meme], store, temp_root=export_dir)
    assert error.value.code == "member_unreadable"
    assert not export_dir.exists()
    assert outside.exists()
