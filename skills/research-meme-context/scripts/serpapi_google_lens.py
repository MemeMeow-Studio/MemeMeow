"""表情包语境检索 Skill 的 SerpApi Google Lens 持久化缓存命令。

脚本位于工作区 Skill 内，面向需要从本地图片发现检索锚点的 Agent。它以图片内容和
检索参数生成稳定缓存键，保存脱敏后的结果快照，避免重复调用付费的图片检索服务。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import mimetypes
import os
import secrets
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


SERPAPI_IMAGE_URL = "https://serpapi.com/image"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
MAX_UPLOAD_BYTES = 500 * 1024
EMPTY_TTL = timedelta(days=3)
CACHE_SCHEMA_VERSION = 1
REMOVED_RESPONSE_KEYS = {
    "api_key",
    "image_id",
    "id",
    "json_endpoint",
    "html_endpoint",
    "google_lens_url",
    "raw_html_file",
    "serpapi_link",
    "serpapi_exact_matches_link",
}


class SerpApiError(RuntimeError):
    """表示 SerpApi 上传或检索失败，消息不包含密钥与临时上传凭据。"""


@dataclass(frozen=True)
class LensRequest:
    """描述一次会影响 Lens 结果的本地图片检索参数。"""

    image_path: Path
    search_type: str = "all"
    language: str = "zh-cn"
    country: str | None = None
    query: str | None = None
    auto_crop: bool = False

    def cache_identity(self, image_sha256: str) -> dict[str, object]:
        """构造缓存键输入，排除文件路径和短期的 SerpApi 上传标识。"""
        return {
            "provider": "serpapi",
            "engine": "google_lens",
            "image_sha256": image_sha256,
            "search_type": self.search_type,
            "language": self.language,
            "country": self.country,
            "query": self.query,
            "auto_crop": self.auto_crop,
        }


class ReverseImageCache:
    """管理单个反向图片检索键的快照、文件锁和原子写入。"""

    def __init__(self, root: Path):
        """初始化缓存目录；`root` 是可持久化的工作区数据路径。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, cache_key: str) -> dict[str, object] | None:
        """读取并基本校验指定缓存记录；损坏记录视为缓存未命中。"""
        path = self._record_path(cache_key)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if not isinstance(value.get("snapshots"), list):
            return None
        sanitized = _sanitize_value(value)
        if not isinstance(sanitized, dict):
            return None
        if sanitized != value:
            # 旧版本可能已保存字段名中包含 serpapi 的归档链接；读取时立即迁移。
            self.write(cache_key, sanitized)
        return sanitized

    def write(self, cache_key: str, record: Mapping[str, object]) -> None:
        """原子写入记录，避免进程中断时产生半截 JSON 文件。"""
        destination = self._record_path(cache_key)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{cache_key}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(record, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, destination)
        finally:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @contextmanager
    def lock(self, cache_key: str) -> Iterator[None]:
        """对同一键互斥执行网络调用，防止并发任务重复产生费用。"""
        lock_path = self.root / f"{cache_key}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _record_path(self, cache_key: str) -> Path:
        """返回由不可逆请求指纹命名的记录文件路径。"""
        return self.root / f"{cache_key}.json"


FetchLensResult = Callable[[LensRequest, str], dict[str, Any]]


def run_lens_search(
    lens_request: LensRequest,
    api_key: str,
    cache_root: Path,
    *,
    refresh: bool = False,
    now: datetime | None = None,
    fetch_result: FetchLensResult | None = None,
) -> dict[str, object]:
    """读取缓存或调用 SerpApi，并返回脱敏结果与缓存状态。

    `refresh` 为真时会追加新的成功快照而非覆盖旧快照。`fetch_result` 仅用于测试，
    生产调用默认走 SerpApi 的上传和 Lens 检索接口。
    """
    image_path = lens_request.image_path.expanduser().resolve()
    _validate_local_image(image_path)
    lens_request = replace(lens_request, image_path=image_path)
    image_sha256 = _sha256_file(image_path)
    request_identity = lens_request.cache_identity(image_sha256)
    cache_key = _fingerprint(request_identity)
    cache = ReverseImageCache(cache_root)
    timestamp = _as_utc(now)
    fetch = fetch_result or _fetch_from_serpapi

    with cache.lock(cache_key):
        record = cache.load(cache_key)
        latest = _latest_snapshot(record)
        if not refresh and _can_reuse(latest, timestamp):
            return _output(cache_key, "hit", latest)

        response = _sanitize_response(fetch(lens_request, api_key), secrets_to_remove=(api_key,))
        _raise_for_unsuccessful_response(response)
        outcome = "empty" if _is_empty(response) else "success"
        snapshot: dict[str, object] = {
            "fetched_at": timestamp.isoformat(),
            "outcome": outcome,
            "expires_at": (timestamp + EMPTY_TTL).isoformat() if outcome == "empty" else None,
            "response": response,
        }
        next_record = _append_snapshot(record, request_identity, snapshot)
        cache.write(cache_key, next_record)
        return _output(cache_key, "refresh" if record else "miss", snapshot)


def _fetch_from_serpapi(lens_request: LensRequest, api_key: str) -> dict[str, Any]:
    """将本地图片上传到 SerpApi，并以临时标识同步查询 Google Lens。"""
    image_id = _upload_image(lens_request.image_path, api_key)
    parameters: dict[str, str] = {
        "engine": "google_lens",
        "image_id": image_id,
        "type": lens_request.search_type,
        "hl": lens_request.language,
        "api_key": api_key,
    }
    if lens_request.country:
        parameters["country"] = lens_request.country
    if lens_request.query:
        parameters["q"] = lens_request.query
    if lens_request.auto_crop:
        parameters["auto_crop"] = "true"
    return _request_json(f"{SERPAPI_SEARCH_URL}?{urlencode(parameters)}")


def _upload_image(image_path: Path, api_key: str) -> str:
    """上传支持格式的本地图片，返回仅用于当前请求的短期 `image_id`。"""
    mime_type = mimetypes.guess_type(image_path.name)[0]
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise SerpApiError("SerpApi 图片上传只支持 JPG/JPEG、PNG 或 WebP")

    boundary = f"----MemeMeow{secrets.token_hex(16)}"
    image_bytes = image_path.read_bytes()
    suffix = image_path.suffix.lower() or ".bin"
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image"; filename="upload' + suffix.encode("ascii") + b'"\r\n',
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        image_bytes,
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="api_key"\r\n\r\n',
        api_key.encode(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    response = _request_json(
        SERPAPI_IMAGE_URL,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    image_id = response.get("image_id")
    if not isinstance(image_id, str) or not image_id:
        raise SerpApiError(_safe_error_message(response, "SerpApi 图片上传失败"))
    return image_id


def _request_json(url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """执行不记录 URL 的 JSON HTTP 请求，并将供应商错误转换为安全异常。"""
    request = Request(url, data=data, headers=dict(headers or {}), method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=60) as response:
            payload = response.read()
    except HTTPError as error:
        payload = error.read()
        raise SerpApiError(_safe_error_message(_decode_json(payload), f"SerpApi 请求失败（HTTP {error.code}）")) from error
    except URLError as error:
        raise SerpApiError("无法连接 SerpApi") from error
    decoded = _decode_json(payload)
    if not isinstance(decoded, dict):
        raise SerpApiError("SerpApi 返回了无法解析的 JSON 响应")
    return decoded


def _decode_json(payload: bytes) -> object:
    """解析 HTTP 响应 JSON；非法响应返回空对象以避免泄露原始内容。"""
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _validate_local_image(image_path: Path) -> None:
    """校验上传文件存在、为常规文件且不超过 SerpApi 的大小上限。"""
    if not image_path.is_file():
        raise SerpApiError("图片文件不存在或不是常规文件")
    if image_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise SerpApiError("图片超过 SerpApi 的 500 KB 上传上限，请先生成临时缩小副本")


def _sha256_file(path: Path) -> str:
    """分块计算图片内容哈希，避免缓存将同名不同图误认为同一次检索。"""
    digest = hashlib.sha256()
    with path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(identity: Mapping[str, object]) -> str:
    """将规范化的无秘密请求参数转换为稳定缓存键。"""
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_response(value: object, *, secrets_to_remove: tuple[str, ...] = ()) -> dict[str, Any]:
    """递归移除 SerpApi 归档地址、临时标识和任何意外回显的密钥。"""
    sanitized = _sanitize_value(value, secrets_to_remove=secrets_to_remove)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_value(value: object, *, secrets_to_remove: tuple[str, ...] = ()) -> object:
    """处理嵌套 JSON 容器，保留候选页面及图片链接供后续核验。"""
    if isinstance(value, dict):
        return {
            key: _sanitize_value(item, secrets_to_remove=secrets_to_remove)
            for key, item in value.items()
            if key not in REMOVED_RESPONSE_KEYS
            and "serpapi" not in key
            and not key.startswith("serpapi_")
            and not key.endswith("_endpoint")
            and not key.endswith("_file")
        }
    if isinstance(value, list):
        return [_sanitize_value(item, secrets_to_remove=secrets_to_remove) for item in value]
    if isinstance(value, str):
        for secret in secrets_to_remove:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def _raise_for_unsuccessful_response(response: Mapping[str, object]) -> None:
    """确保只有完成的 Lens 搜索可进入缓存，避免长期保存失败响应。"""
    metadata = response.get("search_metadata")
    status = metadata.get("status") if isinstance(metadata, Mapping) else None
    if status != "Success":
        raise SerpApiError(_safe_error_message(response, "SerpApi Google Lens 未返回成功结果"))
    if response.get("error"):
        raise SerpApiError(_safe_error_message(response, "SerpApi Google Lens 请求失败"))


def _safe_error_message(response: object, default: str) -> str:
    """返回不包含供应商原始内容的错误文本，防止意外回显密钥。"""
    return default


def _is_empty(response: Mapping[str, object]) -> bool:
    """将没有视觉、精确或相关候选的成功响应判定为短期空结果。"""
    return not any(isinstance(response.get(field), list) and response[field] for field in ("visual_matches", "exact_matches", "related_content"))


def _latest_snapshot(record: Mapping[str, object] | None) -> dict[str, object] | None:
    """返回最新合法快照；旧记录损坏时让调用方重新请求。"""
    if not record:
        return None
    snapshots = record.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return None
    latest = snapshots[-1]
    return latest if isinstance(latest, dict) else None


def _can_reuse(snapshot: Mapping[str, object] | None, now: datetime) -> bool:
    """成功快照默认复用；空结果仅在其短期有效窗口内复用。"""
    if not snapshot:
        return False
    if snapshot.get("outcome") == "success":
        return True
    if snapshot.get("outcome") != "empty" or not isinstance(snapshot.get("expires_at"), str):
        return False
    try:
        return datetime.fromisoformat(snapshot["expires_at"]).astimezone(UTC) > now
    except ValueError:
        return False


def _append_snapshot(
    record: Mapping[str, object] | None,
    request_identity: Mapping[str, object],
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """创建或扩展缓存记录，保留同一查询的历史成功快照以供追溯。"""
    snapshots = list(record.get("snapshots", [])) if record else []
    snapshots.append(dict(snapshot))
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "provider": "serpapi",
        "engine": "google_lens",
        "request": dict(request_identity),
        "snapshots": snapshots,
    }


def _output(cache_key: str, cache_status: str, snapshot: Mapping[str, object]) -> dict[str, object]:
    """生成供 Agent 消费的输出，而不暴露上传凭据或内部归档标识。"""
    return {
        "cache": {
            "key": cache_key,
            "status": cache_status,
            "fetched_at": snapshot.get("fetched_at"),
            "outcome": snapshot.get("outcome"),
            "expires_at": snapshot.get("expires_at"),
        },
        "result": snapshot.get("response"),
    }


def _as_utc(value: datetime | None) -> datetime:
    """规范化测试或运行时传入的时钟值，保证快照时间带 UTC 时区。"""
    timestamp = value or datetime.now(UTC)
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def _project_root() -> Path:
    """从 Skill 脚本位置向上定位工作区根目录，避免依赖当前工作目录。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("无法定位 MemeMeow 工作区根目录")


def _default_cache_root(project_root: Path) -> Path:
    """从环境读取可选缓存路径，否则使用现有持久化 data 根目录。"""
    configured = os.getenv("MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT")
    if configured:
        configured_path = Path(configured).expanduser()
        return configured_path if configured_path.is_absolute() else project_root / configured_path
    data_root = Path(os.getenv("MEMEMEOW_DATA_ROOT", str(project_root / "data"))).expanduser()
    if not data_root.is_absolute():
        data_root = project_root / data_root
    return data_root / "reverse_image_cache" / "serpapi_google_lens"


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """解析本地图片检索 CLI 参数，供 Skill 和自动化任务调用。"""
    parser = argparse.ArgumentParser(description="以 SerpApi Google Lens 检索本地图片，并持久化脱敏结果快照。")
    parser.add_argument("image", type=Path, help="待检索的本地 JPG/JPEG、PNG 或 WebP 图片")
    parser.add_argument("--type", dest="search_type", choices=("all", "about_this_image", "products", "exact_matches", "visual_matches"), default="all")
    parser.add_argument("--hl", dest="language", default="zh-cn", help="Google Lens 返回语言")
    parser.add_argument("--country", help="两位国家代码，例如 jp")
    parser.add_argument("--query", help="有观察依据的附加查询词")
    parser.add_argument("--auto-crop", action="store_true", help="让 Google Lens 自动聚焦图中主体")
    parser.add_argument("--refresh", action="store_true", help="忽略已有成功缓存并追加一条新快照")
    parser.add_argument("--cache-root", type=Path, help="覆盖默认的持久化缓存目录")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    """加载工作区 `.env`，执行一次缓存感知的 Lens 检索并输出 JSON。"""
    project_root = _project_root()
    load_dotenv(project_root / ".env", override=False)
    args = parse_args(arguments)
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("SERPAPI_API_KEY 未配置")
    request = LensRequest(
        image_path=args.image,
        search_type=args.search_type,
        language=args.language,
        country=args.country,
        query=args.query,
        auto_crop=args.auto_crop,
    )
    cache_root = args.cache_root or _default_cache_root(project_root)
    try:
        output = run_lens_search(request, api_key, cache_root, refresh=args.refresh)
    except SerpApiError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
