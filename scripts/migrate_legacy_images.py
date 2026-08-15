"""将图片根目录中的旧图片安全登记到 PostgreSQL local scope。

该脚本是实施期受控迁移入口，不读取旧任务 JSON、搜索缓存或其他历史状态。
图片字节保持原位，数据库 Meme 记录作为迁移后的结构化权威；图片后续通过
现有 ``visual_embedding_generation`` 任务衔接 ``meme_context_generation``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

if __package__ in {None, ""}:
    # 允许维护者直接执行脚本，同时保持 ``python -m`` 的标准入口。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Settings
from backend.database import (
    DatabaseError,
    DatabaseResources,
    Meme,
    Scope,
    Task,
    check_database,
    create_engine_for_settings,
)
from backend.metadata import MetadataError, MetadataService, SidecarMetadata
from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.visual import identity_from_settings


LOGGER = logging.getLogger("mememeow.legacy_migration")
SUPPORTED_FORMATS = {"PNG", "JPEG", "GIF"}
MAX_SIDECAR_BYTES = 1 * 1024 * 1024
DEFAULT_API_URL = "http://127.0.0.1:8275"


@dataclass(frozen=True)
class InspectedImage:
    """通过文件、图片格式和 sidecar 指纹检查的候选图片。"""

    path: Path
    storage_key: str
    extension: str
    size_bytes: int
    sha256: str
    sidecar: SidecarMetadata | None
    sidecar_error: str | None


def _sha256_file(path: Path) -> str:
    """分块计算图片 SHA-256，避免一次性把旧文件全部载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    """生成写入迁移 provenance 的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def _base_context() -> dict[str, object]:
    """构造缺少可信 sidecar 时使用的最小 repair_required 语境。"""
    return {
        "title": None,
        "summary": "",
        "subjects": [],
        "visible_text": [],
        "references": [],
        "meaning": None,
        "keywords": [],
        "search_queries": [],
        "uncertainties": ["旧图片 sidecar 不存在或无法通过完整校验"],
        "source_urls": [],
    }


def _migration_provenance(error: str) -> dict[str, object]:
    """构造不把不可信 sidecar 内容冒充研究结果的迁移 provenance。"""
    return {
        "producer": "legacy_import",
        "model": None,
        "updated_at": _now_iso(),
        "field_sources": {},
        "last_error": error,
    }


def _sidecar_payload(metadata: SidecarMetadata) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """把通过 Pydantic 和图片指纹校验的 sidecar 映射到数据库三段 JSON。"""
    known = {"schema_version", "image", "context_status", "meme_context", "provenance"}
    payload = metadata.model_dump(mode="json", exclude_none=False)
    extensions = {key: value for key, value in payload.items() if key not in known}
    return (
        metadata.meme_context.model_dump(mode="json", exclude_none=False),
        metadata.provenance.model_dump(mode="json", exclude_none=False),
        extensions,
    )


def _load_sidecar(service: MetadataService, image: Path) -> tuple[SidecarMetadata | None, str | None]:
    """严格读取同名 sidecar；失败只标记图片待修复，不读取未经校验的字段。"""
    sidecar = image.with_name(f"{image.name}.json")
    if not sidecar.exists():
        return None, "metadata_missing"
    if sidecar.is_symlink() or not sidecar.is_file():
        return None, "metadata_path_forbidden"
    try:
        if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
            return None, "metadata_too_large"
        return service.load(image), None
    except MetadataError as exc:
        return None, exc.code
    except OSError:
        return None, "metadata_unreadable"


def inspect_image(path: Path, root: Path, *, max_size: int) -> tuple[InspectedImage | None, str | None]:
    """校验单个候选文件的扁平 key、大小、真实格式、可读性和 SHA。"""
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return None, "path_forbidden"
    if path.is_symlink():
        return None, "symlink_forbidden"
    if "/" in relative:
        return None, "non_flat_storage_key"
    try:
        validate_business_storage_key(relative)
    except ValueError as exc:
        return None, str(exc)
    try:
        stat = path.stat()
    except OSError:
        return None, "image_unreadable"
    if stat.st_size > max_size:
        return None, "image_too_large"
    try:
        with Image.open(path) as image:
            if image.format not in SUPPORTED_FORMATS:
                return None, "unsupported_image_format"
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, EOFError):
        return None, "invalid_image"
    try:
        digest = _sha256_file(path)
    except OSError:
        return None, "image_unreadable"
    sidecar, sidecar_error = _load_sidecar(MetadataService(root), path)
    return InspectedImage(path, relative, path.suffix.lower(), stat.st_size, digest, sidecar, sidecar_error), None


def _iter_image_paths(root: Path) -> tuple[list[Path], list[dict[str, str]]]:
    """递归收集图片候选，并记录内部目录和非图片文件的忽略原因。"""
    ignored: list[dict[str, str]] = []
    paths: list[Path] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError:
        return [], [{"path": str(root), "reason": "directory_unreadable"}]
    for path in entries:
        if not path.is_file() or path.is_symlink():
            if path.is_symlink():
                ignored.append({"path": str(path.relative_to(root)), "reason": "symlink_forbidden"})
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_relative_to(root / ".staging") or path.is_relative_to(root / ".quarantine"):
            ignored.append({"path": relative, "reason": "internal_storage"})
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            paths.append(path)
        else:
            ignored.append({"path": relative, "reason": "non_image_file"})
    return paths, ignored


def _candidate_groups(candidates: Iterable[InspectedImage]) -> tuple[list[InspectedImage], list[dict[str, str]]]:
    """按 SHA 去重，优先保留有合法 sidecar 的确定性代表文件。"""
    groups: dict[str, list[InspectedImage]] = defaultdict(list)
    for item in candidates:
        groups[item.sha256].append(item)
    selected: list[InspectedImage] = []
    skipped: list[dict[str, str]] = []
    for sha256, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: item.storage_key)
        valid_sidecars = [item for item in ordered if item.sidecar is not None]
        chosen = valid_sidecars[0] if valid_sidecars else ordered[0]
        # 同一 SHA 的多份合法 sidecar 内容不一致时不擅自选择语境。
        fingerprints = {
            json.dumps(
                item.sidecar.model_dump(mode="json", exclude_none=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in valid_sidecars
        }
        if len(fingerprints) > 1:
            for item in ordered:
                skipped.append({"path": item.storage_key, "reason": "duplicate_sidecar_conflict", "sha256": sha256})
            continue
        selected.append(chosen)
        for item in ordered:
            if item.path != chosen.path:
                skipped.append({"path": item.storage_key, "reason": "duplicate_sha256", "duplicate_of": chosen.storage_key, "sha256": sha256})
    return selected, skipped


def _existing_records(resources: DatabaseResources, scope_id: str) -> list[Meme]:
    """读取目标 scope 的现有 Meme，供路径和内容重复校验使用。"""
    with resources.factory() as session:
        return list(session.scalars(select(Meme).where(Meme.scope_id == scope_id)))


def _create_records(resources: DatabaseResources, inspected: list[InspectedImage], *, scope_id: str, dry_run: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """在单个事务中幂等创建 Meme 记录，不触碰已存在图片字节。"""
    records = _existing_records(resources, scope_id)
    by_key = {item.storage_key: item for item in records}
    by_sha: dict[str, Meme] = {}
    for item in records:
        by_sha.setdefault(item.sha256, item)
    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if dry_run:
        for item in inspected:
            existing = by_key.get(item.storage_key)
            if existing is not None:
                if existing.sha256 == item.sha256 and existing.size_bytes == item.size_bytes:
                    skipped.append({"path": item.storage_key, "reason": "already_registered", "meme_id": str(existing.id)})
                else:
                    failed.append({"path": item.storage_key, "reason": "existing_record_mismatch", "meme_id": str(existing.id)})
                continue
            duplicate = by_sha.get(item.sha256)
            if duplicate is not None:
                skipped.append({"path": item.storage_key, "reason": "duplicate_sha256", "duplicate_of": duplicate.storage_key, "sha256": item.sha256})
                continue
            migrated.append({"path": item.storage_key, "sha256": item.sha256, "size_bytes": item.size_bytes, "sidecar": "valid" if item.sidecar else item.sidecar_error})
            by_sha[item.sha256] = Meme(scope_id=scope_id, storage_key=item.storage_key, sha256=item.sha256, size_bytes=item.size_bytes)
        return migrated, skipped, failed

    with resources.environment(scope_id) as environment:
        for item in inspected:
            existing = by_key.get(item.storage_key)
            if existing is not None:
                if existing.sha256 == item.sha256 and existing.size_bytes == item.size_bytes and existing.extension == item.extension:
                    skipped.append({"path": item.storage_key, "reason": "already_registered", "meme_id": str(existing.id)})
                else:
                    failed.append({"path": item.storage_key, "reason": "existing_record_mismatch", "meme_id": str(existing.id)})
                continue
            duplicate = by_sha.get(item.sha256)
            if duplicate is not None:
                skipped.append({"path": item.storage_key, "reason": "duplicate_sha256", "duplicate_of": duplicate.storage_key, "sha256": item.sha256})
                continue
            if item.sidecar is not None:
                context, provenance, extensions = _sidecar_payload(item.sidecar)
                status = item.sidecar.context_status
                sidecar_status = "valid"
            else:
                context, provenance, extensions = _base_context(), _migration_provenance(item.sidecar_error or "metadata_invalid"), {}
                status = "repair_required"
                sidecar_status = item.sidecar_error or "metadata_invalid"
            try:
                with environment.uow.session.begin_nested():
                    record = environment.memes.create(
                        storage_key=item.storage_key,
                        extension=item.extension,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                        context=context,
                        provenance=provenance,
                        status=status,
                        extensions=extensions,
                    )
            except (DatabaseError, IntegrityError) as exc:
                failed.append({"path": item.storage_key, "reason": getattr(exc, "code", "record_create_failed")})
                continue
            by_key[item.storage_key] = record
            by_sha[item.sha256] = record
            migrated.append({"path": item.storage_key, "meme_id": str(record.id), "sha256": item.sha256, "size_bytes": item.size_bytes, "sidecar": sidecar_status, "context_status": status})
    return migrated, skipped, failed


def _request_json(url: str, payload: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    """调用现有后端批量视觉任务接口并解析 JSON 响应。"""
    request = urllib.request.Request(
        url.rstrip("/") + "/images/visual-embedding/batch",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"api_task_submit_failed:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("api_task_submit_invalid_response")
    return value


def _api_identity(api_url: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """读取后端公开配置摘要，避免把任务交给旧视觉模型空间。"""
    request = urllib.request.Request(api_url.rstrip("/") + "/config", headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _queue_tasks_in_database(resources: DatabaseResources, settings: Settings, meme_ids: list[str], *, scope_id: str) -> list[dict[str, Any]]:
    """在无法唤醒 HTTP Worker 时持久化视觉任务，等待后续服务启动恢复。"""
    identity = identity_from_settings(settings)
    batch_id = f"legacy-import-{uuid4().hex}"
    queued: list[dict[str, Any]] = []
    with resources.environment(scope_id) as environment:
        environment.tasks.ensure_batch(batch_id)
        for meme_id in meme_ids:
            meme = environment.memes.get(meme_id)
            if meme is None:
                queued.append({"meme_id": meme_id, "error": "meme_not_found"})
                continue
            payload = {
                "scope_id": scope_id,
                "meme_id": str(meme.id),
                "image_relative_path": meme.storage_key,
                "image_sha256": meme.sha256,
                "visual_model": identity.model,
                "visual_dimensions": identity.dimensions,
                "preprocess_version": identity.preprocess_version,
                "settings_version": settings.settings_version,
                "auto_name": False,
                "reverse_image_policy": "forbid",
                "batch_id": batch_id,
            }
            dedupe = f"visual:{meme.id}:{meme.sha256}:{identity.model}:{identity.preprocess_version}"
            try:
                task = environment.tasks.submit(task_type="visual_embedding_generation", payload=payload, lane="default", dedupe_key=dedupe, settings_version=settings.settings_version, max_attempts=settings.worker_max_attempts)
                environment.tasks.add_batch_item(batch_id, task.id)
            except DatabaseError as exc:
                queued.append({"meme_id": meme_id, "error": exc.code})
                continue
            queued.append({"meme_id": meme_id, "task_id": task.id, "status": task.status, "batch_id": batch_id})
        environment.tasks.seal_batch(batch_id)
    return queued


def _task_targets(resources: DatabaseResources, settings: Settings, migrated: list[dict[str, Any]], existing_skips: list[dict[str, Any]], *, scope_id: str) -> list[str]:
    """补齐没有当前视觉任务或视觉产物的已登记 Meme，防止中断后漏掉任务。"""
    identity = identity_from_settings(settings)
    meme_ids = [str(item["meme_id"]) for item in migrated if item.get("meme_id")]
    meme_ids.extend(str(item["meme_id"]) for item in existing_skips if item.get("meme_id"))
    if not meme_ids:
        return []
    targets: list[str] = []
    with resources.environment(scope_id) as environment:
        tasks = list(environment.uow.session.scalars(select(Task).where(Task.scope_id == scope_id, Task.task_type == "visual_embedding_generation")))
        for meme_id in dict.fromkeys(meme_ids):
            meme = environment.memes.get(meme_id)
            if meme is None:
                continue
            if environment.visual.get(meme.id, model=identity.model, preprocess_version=identity.preprocess_version, dimensions=identity.dimensions, image_sha256=meme.sha256) is not None:
                continue
            has_current_task = any(
                (payload := dict(task.payload or {})).get("meme_id") == str(meme.id)
                and payload.get("image_sha256") == meme.sha256
                and payload.get("visual_model") == identity.model
                and payload.get("preprocess_version") == identity.preprocess_version
                for task in tasks
            )
            if not has_current_task:
                targets.append(str(meme.id))
    return targets


def submit_visual_tasks(resources: DatabaseResources, settings: Settings, meme_ids: list[str], *, api_url: str | None, scope_id: str, no_tasks: bool) -> dict[str, Any]:
    """为本次新 Meme 提交视觉批次，并保留 visual→context 的现有衔接。"""
    result: dict[str, Any] = {"mode": "none" if no_tasks else "database", "submitted": [], "failed": []}
    if no_tasks or not meme_ids:
        return result
    identity = identity_from_settings(settings)
    if api_url:
        config = _api_identity(api_url)
        try:
            api_dimensions = int(config.get("visual_model_dimensions", -1)) if config else -1
        except (TypeError, ValueError):
            api_dimensions = -1
        compatible = bool(config and config.get("visual_model") == identity.model and api_dimensions == identity.dimensions and config.get("visual_preprocess_version") == identity.preprocess_version)
        if compatible:
            try:
                response = _request_json(api_url, {"items": [{"meme_id": meme_id} for meme_id in meme_ids], "include_unready": True})
                items = response.get("results")
                if isinstance(items, list):
                    result["mode"] = "api"
                    result["batch_id"] = response.get("batch_id")
                    submitted_ids: set[str] = set()
                    for item in items:
                        if isinstance(item, dict) and item.get("task_id"):
                            result["submitted"].append(item)
                            if isinstance(item.get("meme_id"), str):
                                submitted_ids.add(item["meme_id"])
                        else:
                            result["failed"].append(item if isinstance(item, dict) else {"error": "api_task_submit_invalid_item"})
                    if len(result["submitted"]) == len(meme_ids):
                        return result
                    missing_ids = [meme_id for meme_id in meme_ids if meme_id not in submitted_ids]
                    result["submitted"].extend(_queue_tasks_in_database(resources, settings, missing_ids, scope_id=scope_id))
                    result["mode"] = "api+database"
                    return result
            except RuntimeError as exc:
                result["api_error"] = str(exc).split(":", 1)[0]
        else:
            result["api_error"] = "api_visual_identity_mismatch"
    result["mode"] = "database"
    result["submitted"] = _queue_tasks_in_database(resources, settings, meme_ids, scope_id=scope_id)
    return result


def run_migration(args: argparse.Namespace) -> dict[str, Any]:
    """执行一次扫描、去重、数据库登记和异步视觉任务提交。"""
    if args.scope != "local":
        raise RuntimeError("unsupported_scope: legacy root only maps to local scope")
    if args.max_size is not None and args.max_size <= 0:
        raise RuntimeError("max_size_invalid")
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("image_root_unreadable")
    settings = Settings.from_env(args.env_file)
    if args.database_url:
        settings.database_url = args.database_url
    if args.max_size is not None:
        max_size = args.max_size
    else:
        max_size = settings.max_upload_size
    engine = create_engine_for_settings(settings)
    resources: DatabaseResources | None = None
    try:
        check_database(engine, expected_revision=settings.expected_database_revision)
        resources = DatabaseResources(engine, image_root=root, data_root=settings.data_root, settings=settings)
        if resources.blob_store.root != root:
            raise RuntimeError("image_root_mismatch")
        with resources.factory() as session:
            if session.scalar(select(Scope).where(Scope.id == args.scope)) is None:
                raise RuntimeError("scope_not_found")
        paths, ignored = _iter_image_paths(root)
        inspected: list[InspectedImage] = []
        rejected: list[dict[str, str]] = []
        metadata_errors: dict[str, int] = defaultdict(int)
        for path in paths:
            item, reason = inspect_image(path, root, max_size=max_size)
            if item is None:
                rejected.append({"path": path.relative_to(root).as_posix(), "reason": reason or "invalid_image"})
            else:
                inspected.append(item)
                if item.sidecar_error:
                    metadata_errors[item.sidecar_error] += 1
        selected, duplicate_skips = _candidate_groups(inspected)
        migrated, existing_skips, create_failures = _create_records(resources, selected, scope_id=args.scope, dry_run=args.dry_run)
        task_targets = [] if args.dry_run else _task_targets(resources, settings, migrated, existing_skips, scope_id=args.scope)
        report: dict[str, Any] = {
            "scope": args.scope,
            "root": str(root),
            "dry_run": bool(args.dry_run),
            "scanned_image_files": len(paths),
            "validated_image_files": len(inspected),
            "ignored": ignored,
            "rejected": rejected,
            "duplicate_skips": duplicate_skips,
            "metadata_errors": dict(sorted(metadata_errors.items())),
            "migrated": migrated,
            "existing_skips": existing_skips,
            "create_failures": create_failures,
            "task_targets": task_targets,
            "task_submission": {"mode": "none", "submitted": [], "failed": []},
        }
        if not args.dry_run:
            report["task_submission"] = submit_visual_tasks(resources, settings, task_targets, api_url=args.api_url, scope_id=args.scope, no_tasks=args.no_tasks)
        report["counts"] = {
            "migrated_images": len(migrated),
            "skipped_duplicates_or_existing": len(duplicate_skips) + len(existing_skips),
            "ignored_non_images_or_internal": len(ignored),
            "rejected_images": len(rejected),
            "record_failures": len(create_failures),
            "visual_tasks_submitted": len(report["task_submission"].get("submitted", [])),
            "visual_task_submission_failures": len(report["task_submission"].get("failed", [])),
        }
        LOGGER.info("legacy migration scanned=%d migrated=%d rejected=%d duplicates=%d", len(paths), len(migrated), len(rejected), len(duplicate_skips))
        return report
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    """构造迁移脚本命令行参数。"""
    parser = argparse.ArgumentParser(description="安全登记 data/images 旧图片并提交视觉任务")
    parser.add_argument("--root", default="data/images", help="旧图片根目录")
    parser.add_argument("--scope", default="local", help="目标 scope；旧图片根目录只允许 local")
    parser.add_argument("--env-file", default=".env", help="Settings 使用的 dotenv 文件")
    parser.add_argument("--database-url", default=None, help="覆盖 PostgreSQL 连接串")
    parser.add_argument("--api-url", default=os.getenv("MEMEMEOW_API_URL", DEFAULT_API_URL), help="现有后端地址")
    parser.add_argument("--max-size", type=int, default=None, help="单图片最大字节数，默认使用服务配置")
    parser.add_argument("--dry-run", action="store_true", help="只扫描和报告，不写数据库或提交任务")
    parser.add_argument("--no-tasks", action="store_true", help="只登记 Meme，不提交视觉任务")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行迁移并以 JSON 输出完整报告；异常不修改图片文件。"""
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        report = run_migration(args)
    except (DatabaseError, RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc).split(":", 1)[0], "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    report["status"] = "dry_run" if args.dry_run else "completed"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
