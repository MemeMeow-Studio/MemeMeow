#!/usr/bin/env python3
"""读取后端已物化的 task-scoped 视觉候选 manifest。

该脚本只读取当前任务的候选清单，不发起 HTTP 请求、读取 callback 凭据或接受 top-k、
scope 和图片标识。候选排序和数量在任务 claim 前已经冻结。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
SAFE_CONTEXT_FIELDS = frozenset({"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"})
CONTEXT_LIST_FIELDS = frozenset({"subjects", "visible_text", "references", "keywords", "search_queries", "uncertainties"})
CONTEXT_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://")


def _error(code: str, message: str) -> int:
    """将 manifest 读取失败写到 stderr 并返回非零退出码。"""
    print(json.dumps({"error": code, "message": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


def _manifest_path(task_id: str) -> Path:
    """解析可信运行目录下当前任务的 manifest，拒绝替换根目录或任务。"""
    runtime_root = Path(os.path.abspath(os.path.expanduser(os.getenv("MEMEMEOW_DATA_ROOT") or "/runtime")))
    expected = runtime_root / "candidates" / task_id / MANIFEST_NAME
    configured = os.getenv("MEMEMEOW_AGENT_CANDIDATE_MANIFEST")
    if configured:
        path = Path(configured).expanduser()
    else:
        path = expected
    absolute = Path(os.path.abspath(path))
    if absolute != expected:
        raise ValueError("candidate_manifest_path_invalid")
    return absolute


def _read_manifest(path: Path) -> dict[str, Any]:
    """逐级拒绝符号链接并读取有限大小的 JSON manifest。"""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("candidate_manifest_path_invalid")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("candidate_manifest_path_invalid")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("candidate_manifest_invalid")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ValueError("candidate_manifest_too_large")
    with path.open("rb") as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("candidate_manifest_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("candidate_manifest_invalid") from exc
    if not isinstance(value, dict) or set(value) != {"protocol_version", "snapshot_sha256", "matched_at", "candidate_count", "query", "candidates"} or value.get("protocol_version") != 2 or not isinstance(value.get("query"), dict) or set(value["query"]) != {"meme_id", "image_sha256", "model", "dimensions", "preprocess_version"} or not isinstance(value.get("candidates"), list):
        raise ValueError("candidate_manifest_invalid")
    candidates = value["candidates"]
    if not isinstance(value.get("candidate_count"), int) or isinstance(value["candidate_count"], bool) or value["candidate_count"] != len(candidates) or value["candidate_count"] > 50:
        raise ValueError("candidate_manifest_invalid")
    snapshot_hash = value.get("snapshot_sha256")
    if not isinstance(snapshot_hash, str) or not SHA256_RE.fullmatch(snapshot_hash):
        raise ValueError("candidate_manifest_invalid")
    for expected_rank, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict) or set(candidate) != {"rank", "meme_id", "image_sha256", "size_bytes", "score", "relative_path", "context"} or candidate.get("rank") != expected_rank:
            raise ValueError("candidate_manifest_invalid")
        candidate_id = candidate.get("meme_id")
        image_sha = candidate.get("image_sha256")
        relative = candidate.get("relative_path")
        size = candidate.get("size_bytes")
        score = candidate.get("score")
        context = candidate.get("context")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(image_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", image_sha)
            or not isinstance(relative, str)
            or not relative
            or relative == MANIFEST_NAME
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or not -1.000001 <= float(score) <= 1.000001
            or not isinstance(context, dict)
            or set(context) - SAFE_CONTEXT_FIELDS
        ):
            raise ValueError("candidate_manifest_invalid")
        for context_key, context_value in context.items():
            if context_key in CONTEXT_LIST_FIELDS:
                if not isinstance(context_value, list) or any(
                    not isinstance(item, str) or CONTEXT_URL_RE.search(item)
                    for item in context_value
                ):
                    raise ValueError("candidate_manifest_invalid")
            elif context_key not in {"title", "summary", "meaning"} or (
                context_value is not None
                and (not isinstance(context_value, str) or CONTEXT_URL_RE.search(context_value))
            ):
                raise ValueError("candidate_manifest_invalid")
    try:
        snapshot = {key: value[key] for key in ("protocol_version", "query", "matched_at", "candidates")}
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("candidate_manifest_invalid") from exc
    if digest != snapshot_hash:
        raise ValueError("candidate_manifest_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    """读取当前 Agent 任务的固定候选 manifest 并输出 JSON。"""
    task_id = os.getenv("MEMEMEOW_AGENT_TASK_ID", "")
    if not TASK_ID_RE.fullmatch(task_id):
        return _error("agent_task_id_missing", "运行时未注入有效任务标识")
    parser = argparse.ArgumentParser(description="读取当前 Agent 任务的视觉候选 manifest")
    # 不定义候选数量、scope 或图片标识参数，避免 Agent 在运行中扩大候选范围。
    try:
        _args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        return _error("candidate_manifest_arguments_invalid", "候选清单读取不接受候选数量或范围参数")
    if unknown:
        return _error("candidate_manifest_arguments_invalid", "候选清单读取不接受候选数量或范围参数")
    try:
        payload = _read_manifest(_manifest_path(task_id))
    except FileNotFoundError:
        return _error("candidate_manifest_missing", "当前任务没有已准备的视觉候选 manifest")
    except (OSError, ValueError) as exc:
        return _error(str(exc) if str(exc).startswith("candidate_manifest_") else "candidate_manifest_invalid", "视觉候选 manifest 无法读取")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
