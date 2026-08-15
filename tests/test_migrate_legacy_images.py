"""旧图片迁移工具的文件校验和去重边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from backend.metadata import MetadataService
from scripts.migrate_legacy_images import _candidate_groups, _iter_image_paths, inspect_image


def _write_image(path: Path, *, color: tuple[int, int, int] = (20, 40, 60)) -> None:
    """写入一个可被 Pillow 完整校验的 JPEG 测试图片。"""
    Image.new("RGB", (8, 8), color).save(path, format="JPEG")


def test_scan_ignores_json_and_rejects_invalid_image(tmp_path: Path) -> None:
    """扫描只把受支持扩展名交给图片校验，不会把 sidecar 当图片。"""
    _write_image(tmp_path / "valid.jpg")
    (tmp_path / "valid.jpg.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fake.jpg").write_text("not an image", encoding="ascii")

    paths, ignored = _iter_image_paths(tmp_path)
    assert [path.name for path in paths] == ["fake.jpg", "valid.jpg"]
    assert {item["reason"] for item in ignored} == {"non_image_file"}
    valid, valid_error = inspect_image(tmp_path / "valid.jpg", tmp_path, max_size=1024 * 1024)
    invalid, invalid_error = inspect_image(tmp_path / "fake.jpg", tmp_path, max_size=1024 * 1024)
    assert valid is not None and valid_error is None
    assert invalid is None and invalid_error == "invalid_image"


def test_duplicate_sha_prefers_valid_sidecar_without_deleting_files(tmp_path: Path) -> None:
    """同 SHA 文件只选择一个代表，合法 sidecar 优先且源文件仍保留。"""
    first = tmp_path / "z.jpg"
    second = tmp_path / "a.jpg"
    _write_image(first)
    second.write_bytes(first.read_bytes())
    MetadataService(tmp_path).create_pending(second)

    first_item, first_error = inspect_image(first, tmp_path, max_size=1024 * 1024)
    second_item, second_error = inspect_image(second, tmp_path, max_size=1024 * 1024)
    assert first_item is not None and first_error is None
    assert second_item is not None and second_error is None
    selected, skipped = _candidate_groups([first_item, second_item])
    assert [item.storage_key for item in selected] == ["a.jpg"]
    assert skipped == [{"path": "z.jpg", "reason": "duplicate_sha256", "duplicate_of": "a.jpg", "sha256": first_item.sha256}]
    assert first.exists() and second.exists() and (tmp_path / "a.jpg.json").exists()


def test_sidecar_identity_mismatch_is_not_imported_as_context(tmp_path: Path) -> None:
    """sidecar 路径或指纹不匹配时仍可登记图片，但只能进入待修复状态。"""
    image = tmp_path / "mismatch.jpg"
    _write_image(image)
    MetadataService(tmp_path).create_pending(image)
    sidecar = image.with_name(f"{image.name}.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["image"]["sha256"] = "0" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    inspected, error = inspect_image(image, tmp_path, max_size=1024 * 1024)
    assert inspected is not None and error is None
    assert inspected.sidecar is None
    assert inspected.sidecar_error == "metadata_image_mismatch"
