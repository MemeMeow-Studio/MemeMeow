"""MemeMeow 反向图片内部接口薄 CLI。

脚本只解析 Skill 参数、读取任务运行时地址并提交 multipart 请求；缓存、供应商访问、
脱敏和用量计数均由后端完成。Agent 环境不需要也不应包含 ``SERPAPI_API_KEY``。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_UPLOAD_BYTES = 500 * 1024
EMPTY_TTL = timedelta(days=3)
CACHE_SCHEMA_VERSION = 1
REMOVED_RESPONSE_KEYS = {"api_key", "image_id", "id", "json_endpoint", "html_endpoint", "google_lens_url", "raw_html_file", "serpapi_link", "serpapi_exact_matches_link", "about_page_serpapi_link"}


class SerpApiError(RuntimeError):
    """旧缓存 API 的稳定错误；生产 CLI 不读取供应商密钥。"""


@dataclass(frozen=True)
class LensRequest:
    """兼容既有离线缓存测试的本地请求值对象。"""

    image_path: Path
    search_type: str = "all"
    language: str = "zh-cn"
    country: str | None = None
    query: str | None = None
    auto_crop: bool = False

    def cache_identity(self, image_sha256: str) -> dict[str, object]:
        """返回与后端兼容的稳定缓存身份。"""
        return {"provider": "serpapi", "engine": "google_lens", "image_sha256": image_sha256, "search_type": self.search_type, "language": self.language, "country": self.country, "query": self.query, "auto_crop": self.auto_crop}


class ReverseImageCache:
    """兼容旧快照 schema 的离线缓存读写和同键文件锁。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, key: str) -> dict[str, object] | None:
        try:
            value = json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(value.get("snapshots"), list):
            return None
        sanitized = _sanitize(value)
        if sanitized != value:
            self.write(key, sanitized)
        return sanitized

    def write(self, key: str, record: Mapping[str, object]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, prefix=f".{key}.", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.root / f"{key}.json")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        with (self.root / f"{key}.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _sanitize(value: object, secrets_to_remove: tuple[str, ...] = ()) -> object:
    """兼容旧快照的递归脱敏。"""
    if isinstance(value, dict):
        return {str(key): _sanitize(item, secrets_to_remove) for key, item in value.items() if str(key) not in REMOVED_RESPONSE_KEYS and "serpapi" not in str(key).lower() and not str(key).endswith(("_endpoint", "_file"))}
    if isinstance(value, list):
        return [_sanitize(item, secrets_to_remove) for item in value]
    if isinstance(value, str):
        for secret in secrets_to_remove:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def _sha256(path: Path) -> str:
    """计算测试图片内容指纹。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(identity: Mapping[str, object]) -> str:
    """计算兼容缓存键。"""
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_lens_search(request: LensRequest, api_key: str, cache_root: Path, *, refresh: bool = False, now: datetime | None = None, fetch_result: Callable[[LensRequest, str], dict[str, object]] | None = None) -> dict[str, object]:
    """保留旧离线缓存测试入口；生产执行路径使用薄 CLI 的内部接口。"""
    timestamp = now or datetime.now(UTC)
    image_sha = _sha256(request.image_path)
    key = _fingerprint(request.cache_identity(image_sha))
    cache = ReverseImageCache(cache_root)
    with cache.lock(key):
        record = cache.load(key)
        snapshots = record.get("snapshots", []) if record else []
        latest = snapshots[-1] if snapshots and isinstance(snapshots[-1], dict) else None
        reusable = bool(latest and latest.get("outcome") == "success")
        if latest and latest.get("outcome") == "empty" and isinstance(latest.get("expires_at"), str):
            try:
                reusable = datetime.fromisoformat(str(latest["expires_at"])).astimezone(UTC) > timestamp
            except ValueError:
                reusable = False
        if reusable and not refresh:
            return {"cache": {"key": key, "status": "hit", "outcome": latest.get("outcome")}, "result": latest.get("response")}
        if fetch_result is None:
            raise SerpApiError("生产 CLI 必须通过内部反向图片接口调用")
        response = _sanitize(fetch_result(request, api_key), (api_key,))
        if not isinstance(response, dict) or (isinstance(response.get("search_metadata"), dict) and response["search_metadata"].get("status") != "Success") or response.get("error"):
            raise SerpApiError("反向图片服务返回了无效结果")
        outcome = "empty" if not any(isinstance(response.get(field), list) and response[field] for field in ("visual_matches", "exact_matches", "related_content")) else "success"
        snapshot = {"fetched_at": timestamp.isoformat(), "outcome": outcome, "expires_at": (timestamp + EMPTY_TTL).isoformat() if outcome == "empty" else None, "response": response}
        next_record = {"schema_version": CACHE_SCHEMA_VERSION, "provider": "serpapi", "engine": "google_lens", "request": request.cache_identity(image_sha), "snapshots": [*snapshots, snapshot]}
        cache.write(key, next_record)
        return {"cache": {"key": key, "status": "refresh" if record else "miss", "outcome": outcome}, "result": response}


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """解析与旧命令兼容的图片和 Lens 检索参数。"""
    parser = argparse.ArgumentParser(description="通过 MemeMeow 内部接口执行反向图片检索")
    parser.add_argument("image", type=Path, help="待检索图片")
    parser.add_argument("--type", dest="search_type", choices=("all", "about_this_image", "products", "exact_matches", "visual_matches"), default="all")
    parser.add_argument("--hl", dest="language", default="zh-cn")
    parser.add_argument("--country")
    parser.add_argument("--query")
    parser.add_argument("--auto-crop", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--task-id", default=os.getenv("MEMEMEOW_AGENT_TASK_ID"))
    parser.add_argument("--request-id", default=None)
    return parser.parse_args(arguments)


def _multipart(fields: dict[str, str], image: bytes, filename: str) -> tuple[bytes, str]:
    """构造不记录密钥的 multipart 请求体。"""
    boundary = "----MemeMeowInternalBoundary"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value.encode(), b"\r\n"])
    chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="image"; filename="{Path(filename).name}"\r\n'.encode(), b"Content-Type: application/octet-stream\r\n\r\n", image, b"\r\n", f"--{boundary}--\r\n".encode()])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def main(arguments: list[str] | None = None) -> int:
    """读取受限图片、提交后端接口并原样输出统一 JSON。"""
    args = parse_args(arguments)
    if not args.task_id:
        print("缺少 MEMEMEOW_AGENT_TASK_ID，请从任务运行环境调用", file=sys.stderr)
        return 2
    try:
        image = args.image.expanduser().resolve()
        content = image.read_bytes()
    except OSError:
        print("图片文件不存在或无法读取", file=sys.stderr)
        return 2
    if len(content) > MAX_UPLOAD_BYTES:
        print("图片超过 500 KB 上传限制", file=sys.stderr)
        return 2
    fields = {"task_id": args.task_id, "search_type": args.search_type, "language": args.language, "auto_crop": str(bool(args.auto_crop)).lower(), "refresh": str(bool(args.refresh)).lower()}
    for name in ("country", "query", "request_id"):
        value = getattr(args, name)
        if value:
            fields[name] = value
    # 端点和短期 callback 凭据只允许由 Runner 注入，避免 Agent 用命令行参数
    # 把图片改发到任意外部地址，或绕过当前 Task claim。
    endpoint = os.getenv("MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL", "http://127.0.0.1:8275/internal/reverse-image/search")
    callback_token = os.getenv("MEMEMEOW_AGENT_CALLBACK_TOKEN")
    if not callback_token:
        print("缺少当前任务 callback 凭据，请从 Runner 注入", file=sys.stderr)
        return 2
    body, content_type = _multipart(fields, content, image.name)
    try:
        response = urlopen(Request(endpoint, data=body, headers={"Content-Type": content_type, "Accept": "application/json", "X-MemeMeow-Callback": callback_token}, method="POST"), timeout=60)
        payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"error": "reverse_image_request_failed", "message": "反向图片接口请求失败"}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("反向图片接口暂时不可用", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
