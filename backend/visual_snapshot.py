"""视觉候选 snapshot 的版本化 canonicalization 和完整性校验。

该模块位于视觉匹配与任务持久化之间，只处理可序列化的任务事实，不读取数据库、
图片文件或运行时路径。snapshot 由后端生成并供 Task、attempt 和 workspace 共同引用。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


VISUAL_MATCH_SNAPSHOT_PROTOCOL_VERSION = 2
MAX_VISUAL_MATCH_CANDIDATES = 50
SNAPSHOT_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
SNAPSHOT_FIELDS = frozenset({"protocol_version", "snapshot_sha256", "matched_at", "query", "candidates"})
QUERY_FIELDS = frozenset({"meme_id", "image_sha256", "model", "dimensions", "preprocess_version"})
CANDIDATE_FIELDS = frozenset({"rank", "meme_id", "image_sha256", "size_bytes", "score", "relative_path", "context"})
MANIFEST_FIELDS = frozenset({"protocol_version", "snapshot_sha256", "matched_at", "candidate_count", "query", "candidates"})
SAFE_CONTEXT_FIELDS = frozenset({"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"})
CONTEXT_LIST_FIELDS = frozenset({"subjects", "visible_text", "references", "keywords", "search_queries", "uncertainties"})
CONTEXT_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://")


class VisualMatchSnapshotError(ValueError):
    """snapshot 输入或持久化内容不符合版本化契约时使用的稳定错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定错误码，供任务服务映射而不暴露底层校验细节。"""
        super().__init__(message or code)
        self.code = code


def _string(value: object, *, code: str, name: str, maximum: int = 255) -> str:
    """校验非空短字符串，拒绝控制字符和不受约束的超长字段。"""
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 0x20 for char in value):
        raise VisualMatchSnapshotError(code, f"snapshot {name} 无效")
    return value


def _sha256(value: object, *, code: str, name: str) -> str:
    """校验图片或 snapshot 的十六进制 SHA-256 字符串。"""
    if not isinstance(value, str) or not IMAGE_SHA256_RE.fullmatch(value):
        raise VisualMatchSnapshotError(code, f"snapshot {name} 无效")
    return value.lower()


def _relative_path(value: object) -> str:
    """校验 task-relative 候选文件名，拒绝绝对路径、跳转和控制字符。"""
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or any(ord(char) < 0x20 for char in value):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选相对路径无效")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选相对路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选相对路径无效")
    if value == "manifest.json":
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选相对路径占用 manifest 文件")
    return value


def _canonical_json(value: Mapping[str, object]) -> bytes:
    """将已校验 snapshot 转为禁止 NaN 的稳定 JSON 字节序列。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 不是可序列化 JSON") from exc


def _snapshot_digest(value: Mapping[str, object]) -> str:
    """计算不包含自身 hash 字段的 snapshot canonical SHA-256。"""
    payload = copy.deepcopy(dict(value))
    payload.pop("snapshot_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_context(value: object) -> dict[str, object]:
    """保留可供研究参考的固定语境字段，并移除 URL 与内部扩展。"""
    if not isinstance(value, Mapping):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 context 无效")
    result: dict[str, object] = {}
    for key in SAFE_CONTEXT_FIELDS:
        if key not in value:
            continue
        raw = value[key]
        if key in CONTEXT_LIST_FIELDS:
            if not isinstance(raw, list):
                raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 context 列表无效")
            cleaned: list[str] = []
            for item in raw:
                if not isinstance(item, str) or any(ord(char) < 0x20 or ord(char) == 0x7F for char in item):
                    raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 context 文本无效")
                if not CONTEXT_URL_RE.search(item):
                    cleaned.append(item)
            result[key] = cleaned
            continue
        if raw is not None and not isinstance(raw, str):
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 context 文本无效")
        if isinstance(raw, str) and (any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw) or CONTEXT_URL_RE.search(raw)):
            continue
        result[key] = raw
    try:
        copied = copy.deepcopy(result)
        _canonical_json(copied)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 context 不是可序列化 JSON") from exc
    return copied


def _candidate(value: Mapping[str, object], *, rank: int) -> dict[str, object]:
    """校验并复制一个候选，屏蔽 storage key、URL 等非 snapshot 字段。"""
    if not isinstance(value, Mapping):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选结构无效")
    candidate_id = _string(value.get("meme_id"), code="visual_match_snapshot_invalid", name="meme_id")
    image_sha256 = _sha256(value.get("image_sha256"), code="visual_match_snapshot_invalid", name="image_sha256")
    raw_size = value.get("size_bytes")
    if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 size_bytes 无效")
    raw_score = value.get("score")
    if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool) or not math.isfinite(float(raw_score)) or not -1.000001 <= float(raw_score) <= 1.000001:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 score 无效")
    copied_context = _canonical_context(value.get("context", {}))
    return {
        "rank": rank,
        "meme_id": candidate_id,
        "image_sha256": image_sha256,
        "size_bytes": raw_size,
        "score": float(raw_score),
        "relative_path": _relative_path(value.get("relative_path")),
        "context": copied_context,
    }


def build_visual_match_snapshot(
    *,
    query_meme_id: object,
    image_sha256: object,
    model: object,
    dimensions: object,
    preprocess_version: object,
    candidates: Sequence[Mapping[str, object]],
    matched_at: datetime | str,
) -> dict[str, object]:
    """构造 protocol v2 snapshot，并返回带完整性 hash 的深拷贝。

    输入是后端已经完成 scope、模型和存储校验的查询及候选；输出可直接写入 Task
    JSONB。候选会按 score 降序、Meme ID 升序重排，重复 Meme ID 会被拒绝。
    """
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot dimensions 无效")
    if isinstance(matched_at, datetime):
        if matched_at.tzinfo is None:
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot matched_at 缺少时区")
        matched_value = matched_at.isoformat()
    else:
        matched_value = _string(matched_at, code="visual_match_snapshot_invalid", name="matched_at", maximum=64)
        try:
            datetime.fromisoformat(matched_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot matched_at 无效") from exc
    query = {
        "meme_id": _string(query_meme_id, code="visual_match_snapshot_invalid", name="query meme_id"),
        "image_sha256": _sha256(image_sha256, code="visual_match_snapshot_invalid", name="query image_sha256"),
        "model": _string(model, code="visual_match_snapshot_invalid", name="model"),
        "dimensions": dimensions,
        "preprocess_version": _string(preprocess_version, code="visual_match_snapshot_invalid", name="preprocess_version", maximum=128),
    }
    prepared: list[dict[str, object]] = []
    seen: set[str] = set()
    seen_paths: set[str] = set()
    if not isinstance(candidates, Sequence) or len(candidates) > MAX_VISUAL_MATCH_CANDIDATES:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 候选数量超出限制")
    for item in candidates:
        candidate_id = item.get("meme_id") if isinstance(item, Mapping) else None
        candidate_key = _string(candidate_id, code="visual_match_snapshot_invalid", name="candidate meme_id")
        if candidate_key in seen:
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 候选重复")
        seen.add(candidate_key)
        candidate = _candidate(item, rank=0)
        relative_path = str(candidate["relative_path"])
        if relative_path in seen_paths:
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 候选路径重复")
        seen_paths.add(relative_path)
        prepared.append(candidate)
    prepared.sort(key=lambda item: (-float(item["score"]), str(item["meme_id"])))
    for index, item in enumerate(prepared, start=1):
        item["rank"] = index
    snapshot: dict[str, object] = {
        "protocol_version": VISUAL_MATCH_SNAPSHOT_PROTOCOL_VERSION,
        "query": query,
        "matched_at": matched_value,
        "candidates": prepared,
    }
    snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
    return copy.deepcopy(snapshot)


def validate_visual_match_snapshot(value: object, *, expected_sha256: str | None = None) -> dict[str, object]:
    """完整校验持久 snapshot 并返回不可被调用方修改的副本。

    输入来自 JSONB 或 resume attempt；输出保证 protocol、排序、候选字段和 hash
    均符合当前契约。``expected_sha256`` 用于 claim 恢复时拒绝错配事实。
    """
    if not isinstance(value, Mapping) or set(value) != SNAPSHOT_FIELDS or value.get("protocol_version") != VISUAL_MATCH_SNAPSHOT_PROTOCOL_VERSION:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot protocol 版本无效")
    query = value.get("query")
    if not isinstance(query, Mapping) or set(query) != QUERY_FIELDS:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot query 无效")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot candidates 无效")
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != CANDIDATE_FIELDS:
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 候选字段无效")
        normalized_context = _canonical_context(candidate.get("context"))
        if normalized_context != candidate.get("context"):
            raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot 候选 context 含非公开字段")
    rebuilt = build_visual_match_snapshot(
        query_meme_id=query.get("meme_id"),
        image_sha256=query.get("image_sha256"),
        model=query.get("model"),
        dimensions=query.get("dimensions"),
        preprocess_version=query.get("preprocess_version"),
        candidates=candidates,
        matched_at=value.get("matched_at"),
    )
    supplied = value.get("snapshot_sha256")
    if not isinstance(supplied, str) or not SNAPSHOT_SHA256_RE.fullmatch(supplied) or supplied != rebuilt["snapshot_sha256"]:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot hash 无效")
    if expected_sha256 is not None and supplied != expected_sha256:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "snapshot hash 与任务事实不一致")
    return rebuilt


def visual_match_snapshot_summary(value: object, *, expected_sha256: str | None = None) -> dict[str, object]:
    """返回公开任务 DTO 可使用的脱敏 snapshot 摘要。"""
    snapshot = validate_visual_match_snapshot(value, expected_sha256=expected_sha256)
    return {
        "protocol_version": snapshot["protocol_version"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "matched_at": snapshot["matched_at"],
        "candidate_count": len(snapshot["candidates"]),
    }


def visual_match_snapshot_manifest(value: object, *, expected_sha256: str | None = None) -> dict[str, object]:
    """返回可写入候选目录的 manifest，保留语境但不带物理存储信息。"""
    snapshot = validate_visual_match_snapshot(value, expected_sha256=expected_sha256)
    return {
        "protocol_version": snapshot["protocol_version"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "matched_at": snapshot["matched_at"],
        "candidate_count": len(snapshot["candidates"]),
        "query": copy.deepcopy(snapshot["query"]),
        "candidates": copy.deepcopy(snapshot["candidates"]),
    }


def validate_visual_match_snapshot_manifest(value: object, *, expected_sha256: str | None = None) -> dict[str, object]:
    """校验候选目录 manifest，并返回与 snapshot 一致的脱敏副本。

    输入来自宿主物化器写入的普通文件；除摘要字段外，manifest 必须完整保留
    同一 protocol v2 snapshot 的查询和候选事实，供 Runner 在启动外部进程前复核。
    """
    if not isinstance(value, Mapping) or set(value) != MANIFEST_FIELDS:
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 manifest 字段无效")
    candidates = value.get("candidates")
    candidate_count = value.get("candidate_count")
    if not isinstance(candidates, list) or not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or candidate_count != len(candidates):
        raise VisualMatchSnapshotError("visual_match_snapshot_invalid", "候选 manifest 数量无效")
    snapshot = {
        "protocol_version": value.get("protocol_version"),
        "snapshot_sha256": value.get("snapshot_sha256"),
        "matched_at": value.get("matched_at"),
        "query": value.get("query"),
        "candidates": candidates,
    }
    validated = validate_visual_match_snapshot(snapshot, expected_sha256=expected_sha256)
    return visual_match_snapshot_manifest(validated, expected_sha256=expected_sha256)


__all__ = [
    "VISUAL_MATCH_SNAPSHOT_PROTOCOL_VERSION",
    "MAX_VISUAL_MATCH_CANDIDATES",
    "VisualMatchSnapshotError",
    "build_visual_match_snapshot",
    "validate_visual_match_snapshot",
    "visual_match_snapshot_summary",
    "visual_match_snapshot_manifest",
    "validate_visual_match_snapshot_manifest",
]
