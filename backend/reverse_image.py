"""后端反向图片检索服务。

该模块位于 FastAPI 内部接口与供应商之间，负责任务策略校验、旧缓存兼容、同键互斥、
SerpApi 适配、敏感字段脱敏以及 usage event 的事实记录。Agent 只接触这里返回的
供应商无关 JSON，不会获得供应商密钥或临时上传标识。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import secrets
import tempfile
from io import BytesIO
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select

from backend.config import Settings
from backend.database import DatabaseError, DatabaseResources, ReverseImageUsageEvent, Task


MAX_UPLOAD_BYTES = 500 * 1024
CACHE_SCHEMA_VERSION = 1
EMPTY_TTL = timedelta(days=3)
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
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
    "about_page_serpapi_link",
}


class ReverseImageError(RuntimeError):
    """内部检索稳定错误，不携带供应商原始正文、密钥或临时标识。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class ReverseImageRequest:
    """一次逻辑反向图片检索的规范化输入。"""

    image: bytes
    filename: str
    task_id: str
    request_id: str | None = None
    search_type: str = "all"
    language: str = "zh-cn"
    country: str | None = None
    query: str | None = None
    auto_crop: bool = False
    refresh: bool = False

    def normalized(self) -> "ReverseImageRequest":
        """校验文件格式和检索参数，返回不含外围空白的规范化请求。"""
        name = Path(self.filename or "image").name
        mime_type = mimetypes.guess_type(name)[0]
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ReverseImageError("invalid_image_format", "图片格式不受支持", status_code=400)
        if not self.image or len(self.image) > MAX_UPLOAD_BYTES:
            raise ReverseImageError("image_too_large", "图片超过 500 KB 上传限制", status_code=413)
        try:
            from PIL import Image

            with Image.open(BytesIO(self.image)) as decoded:
                if decoded.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise ValueError("unsupported")
                decoded.verify()
        except Exception as exc:  # noqa: BLE001
            raise ReverseImageError("invalid_image", "上传内容不是有效图片", status_code=400) from exc
        if self.search_type not in {"all", "about_this_image", "products", "exact_matches", "visual_matches"}:
            raise ReverseImageError("invalid_search_type", "检索类型无效", status_code=400)
        language = self.language.strip()[:32] or "zh-cn"
        country = self.country.strip().lower()[:8] if self.country else None
        query = self.query.strip()[:200] if self.query else None
        return ReverseImageRequest(
            image=self.image,
            filename=name,
            task_id=self.task_id.strip(),
            request_id=self.request_id.strip()[:128] if self.request_id else None,
            search_type=self.search_type,
            language=language,
            country=country,
            query=query,
            auto_crop=bool(self.auto_crop),
            refresh=bool(self.refresh),
        )

    def identity(self, image_sha256: str) -> dict[str, object]:
        """构造不包含路径、策略或密钥的稳定缓存身份。"""
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
    """读取旧版快照 schema，并以同键文件锁保证并发供应商调用互斥。"""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        """返回由 SHA-256 缓存键命名的记录路径。"""
        return self.root / f"{key}.json"

    def load(self, key: str) -> dict[str, object] | None:
        """读取并脱敏校验缓存记录；损坏或不兼容记录视为未命中。"""
        try:
            value = json.loads(self.path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(value.get("snapshots"), list):
            return None
        sanitized = sanitize_value(value)
        if not isinstance(sanitized, dict):
            return None
        if sanitized != value:
            self.write(key, sanitized)
        return sanitized

    def write(self, key: str, value: Mapping[str, object]) -> None:
        """通过 fsync 和原子替换写入脱敏快照。"""
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, prefix=f".{key}.", suffix=".tmp", delete=False) as handle:
                temporary_path = Path(handle.name)
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path(key))
        except OSError as exc:
            raise ReverseImageError("cache_write_failed", "反向图片缓存无法保存", retryable=True, status_code=503) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """在同一缓存键范围内串行执行二次检查和供应商调用。"""
        with (self.root / f"{key}.lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def sanitize_value(value: object, *, secrets_to_remove: tuple[str, ...] = ()) -> object:
    """递归删除供应商密钥、image_id 和私有归档字段。"""
    if isinstance(value, dict):
        return {
            str(key): sanitize_value(item, secrets_to_remove=secrets_to_remove)
            for key, item in value.items()
            if str(key) not in REMOVED_RESPONSE_KEYS
            and "serpapi" not in str(key).lower()
            and not str(key).endswith(("_endpoint", "_file"))
        }
    if isinstance(value, list):
        return [sanitize_value(item, secrets_to_remove=secrets_to_remove) for item in value]
    if isinstance(value, str):
        for secret in secrets_to_remove:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        # 供应商归档 URL 可能出现在普通字段值中，不能只依赖字段名过滤。
        if "serpapi.com/" in value.lower():
            return "[REDACTED]"
    return value


def _fingerprint(identity: Mapping[str, object]) -> str:
    """把无秘密缓存身份编码为稳定 SHA-256。"""
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _latest(record: Mapping[str, object] | None) -> dict[str, object] | None:
    """返回缓存中的最新快照。"""
    snapshots = record.get("snapshots") if record else None
    latest = snapshots[-1] if isinstance(snapshots, list) and snapshots else None
    return latest if isinstance(latest, dict) else None


def _reusable(snapshot: Mapping[str, object] | None, now: datetime) -> bool:
    """成功结果永久可复用，空结果只在 TTL 内复用。"""
    if not snapshot:
        return False
    if snapshot.get("outcome") == "success":
        return True
    if snapshot.get("outcome") != "empty" or not isinstance(snapshot.get("expires_at"), str):
        return False
    try:
        return datetime.fromisoformat(str(snapshot["expires_at"])).astimezone(UTC) > now
    except ValueError:
        return False


def _is_empty(response: Mapping[str, object]) -> bool:
    """判断供应商成功响应是否没有候选结果。"""
    return not any(isinstance(response.get(field), list) and response[field] for field in ("visual_matches", "exact_matches", "related_content"))


class SerpApiGoogleLensProvider:
    """供应商适配器；上传与 Lens 查询合并为一次逻辑调用。"""

    image_url = "https://serpapi.com/image"
    search_url = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, *, timeout: int = 60):
        self.api_key = api_key
        self.timeout = timeout

    def search(self, request: ReverseImageRequest) -> dict[str, Any]:
        """调用 SerpApi 上传图片并查询 Google Lens，统一转换失败。"""
        mime_type = mimetypes.guess_type(request.filename)[0] or "application/octet-stream"
        boundary = f"----MemeMeow{secrets.token_hex(16)}"
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="upload{Path(request.filename).suffix.lower()}"\r\n'.encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            request.image,
            b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"api_key\"\r\n\r\n".encode(),
            self.api_key.encode(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        try:
            uploaded = self._request_json(self.image_url, data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            image_id = uploaded.get("image_id")
            if not isinstance(image_id, str) or not image_id:
                raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效上传结果", retryable=True, status_code=503)
            params = {"engine": "google_lens", "image_id": image_id, "type": request.search_type, "hl": request.language, "api_key": self.api_key}
            if request.country:
                params["country"] = request.country
            if request.query:
                params["q"] = request.query
            if request.auto_crop:
                params["auto_crop"] = "true"
            response = self._request_json(f"{self.search_url}?{urlencode(params)}")
        except ReverseImageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReverseImageError("reverse_image_provider_unavailable", "反向图片服务暂时不可用", retryable=True, status_code=503) from exc
        response = sanitize_value(response, secrets_to_remove=(self.api_key,))
        if not isinstance(response, dict) or response.get("error") or (isinstance(response.get("search_metadata"), dict) and response["search_metadata"].get("status") not in {None, "Success"}):
            raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
        return response

    def _request_json(self, url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        """执行 HTTP JSON 请求且不把 URL、响应正文写入异常。"""
        request = Request(url, data=data, headers=dict(headers or {}), method="POST" if data is not None else "GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            raise ReverseImageError("reverse_image_provider_unavailable", "反向图片服务请求失败", retryable=True, status_code=503) from exc
        except URLError as exc:
            raise ReverseImageError("reverse_image_provider_unavailable", "反向图片服务暂时不可用", retryable=True, status_code=503) from exc
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无法解析的结果", retryable=True, status_code=503) from exc
        if not isinstance(value, dict):
            raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
        return value


class ReverseImageService:
    """执行任务级策略、缓存互斥、供应商访问和 usage event 编排。"""

    def __init__(self, settings: Settings, resources: DatabaseResources, *, provider: Callable[[ReverseImageRequest], dict[str, Any]] | None = None):
        self.settings = settings
        self.resources = resources
        self.cache = ReverseImageCache(settings.reverse_image_cache_root or settings.data_root / "reverse_image_cache" / "serpapi_google_lens")
        self._provider_factory = provider

    @property
    def available(self) -> bool:
        """返回不含密钥的供应商可用状态。"""
        return bool(self.settings.serpapi_api_key or self._provider_factory)

    @staticmethod
    def _locked_auto_task(task: Task | None) -> Task:
        """在缓存键锁内重新确认任务仍可执行且策略为 auto。

        缓存锁可能等待另一个进程完成供应商调用；等待期间任务状态或持久化策略
        可能已经变化，因此不能复用锁外的快照作为授权依据。
        """
        if task is None or task.task_type != "meme_context_generation":
            raise ReverseImageError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
        if task.status != "running":
            raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
        policy = str((task.payload or {}).get("reverse_image_policy") or "forbid")
        if policy != "auto":
            raise ReverseImageError("reverse_image_forbidden", "当前任务禁止反向图片检索", status_code=403)
        return task

    def search(self, request: ReverseImageRequest) -> dict[str, object]:
        """按 task_id 校验运行任务并执行一次供应商无关逻辑检索。"""
        request = request.normalized()
        image_sha = hashlib.sha256(request.image).hexdigest()
        key = _fingerprint(request.identity(image_sha))
        request_id = request.request_id or secrets.token_urlsafe(24)
        with self.resources.environment("local") as environment:
            task = environment.tasks.get(request.task_id, for_update=False)
            if task is None or task.task_type != "meme_context_generation":
                raise ReverseImageError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
            # 先恢复同一 request_id，已完成或中断事件都不得因重试再次联系供应商。
            existing = environment.reverse_image_usage.get(request_id, for_update=True)
            if existing is not None:
                if existing.task_id != request.task_id or existing.cache_key != key:
                    raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409)
                return self._event_output(existing)
            if task.status != "running":
                raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
            policy = str((task.payload or {}).get("reverse_image_policy") or "forbid")
            if policy != "auto":
                event = environment.reverse_image_usage.create(request_id=request_id, task_id=task.id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="miss")
                environment.reverse_image_usage.finish(event.request_id, outcome="forbidden", result={"used": False})
                # 异常响应会触发 UOW 回滚；先提交禁止请求的审计记录，确保拒绝也可追溯。
                environment.uow.session.commit()
                raise ReverseImageError("reverse_image_forbidden", "当前任务禁止反向图片检索", status_code=403)

        timestamp = datetime.now(UTC)
        with self.cache.lock(key):
            record = self.cache.load(key)
            snapshot = _latest(record)
            if not request.refresh and _reusable(snapshot, timestamp):
                with self.resources.environment("local") as environment:
                    task = self._locked_auto_task(environment.tasks.get(request.task_id))
                    event = environment.reverse_image_usage.create(request_id=request_id, task_id=request.task_id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="hit")
                    event = environment.reverse_image_usage.finish(event.request_id, cache_status="hit", outcome="success", result={"used": bool(snapshot and snapshot.get("outcome") == "success"), "snapshot": snapshot})
                    return self._event_output(event)
            with self.resources.environment("local") as environment:
                task = self._locked_auto_task(environment.tasks.get(request.task_id))
                if not self.available:
                    raise ReverseImageError("reverse_image_unavailable", "反向图片服务尚未配置", status_code=503)
                event = environment.reverse_image_usage.create(request_id=request_id, task_id=request.task_id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="refresh" if record else "miss", provider="serpapi")
                if event.task_id != request.task_id or event.cache_key != key:
                    raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409)
                if event.completed_at is not None:
                    return self._event_output(event)
                if event.provider_called:
                    # 进程可能在供应商调用后中断；未知结果只保留已计数状态，不自动重放付费请求。
                    return self._event_output(event)
                environment.reverse_image_usage.mark_provider_started(event.request_id)
            try:
                provider = self._provider_factory or SerpApiGoogleLensProvider(str(self.settings.serpapi_api_key))
                response = provider(request) if callable(provider) else provider.search(request)
                outcome = "empty" if _is_empty(response) else "success"
                snapshot = {"fetched_at": timestamp.isoformat(), "outcome": outcome, "expires_at": (timestamp + EMPTY_TTL).isoformat() if outcome == "empty" else None, "response": sanitize_value(response)}
                next_record = {"schema_version": CACHE_SCHEMA_VERSION, "provider": "serpapi", "engine": "google_lens", "request": request.identity(image_sha), "snapshots": [*(record or {}).get("snapshots", []), snapshot]}
                self.cache.write(key, next_record)
            except ReverseImageError as exc:
                with self.resources.environment("local") as environment:
                    event = environment.reverse_image_usage.finish(request_id, outcome="failed", retryable=exc.retryable, error={"error": exc.code})
                raise
            except Exception as exc:  # noqa: BLE001
                # 供应商适配器异常也必须收束 started 事件，避免永久悬挂且不回显原始正文。
                error = ReverseImageError("reverse_image_provider_unavailable", "反向图片服务暂时不可用", retryable=True, status_code=503)
                with self.resources.environment("local") as environment:
                    environment.reverse_image_usage.finish(request_id, outcome="failed", retryable=True, error={"error": error.code})
                raise error from exc
            with self.resources.environment("local") as environment:
                event = environment.reverse_image_usage.finish(request_id, cache_status="refresh" if record else "miss", outcome=outcome, result={"used": outcome == "success", "snapshot": snapshot})
                return self._event_output(event, snapshot=snapshot)

    @staticmethod
    def _event_output(event: ReverseImageUsageEvent, *, snapshot: Mapping[str, object] | None = None) -> dict[str, object]:
        """将事件映射为稳定供应商无关 JSON。"""
        payload = event.result or {}
        selected = snapshot or payload.get("snapshot")
        return {
            "request_id": event.request_id,
            "cache": {"key": event.cache_key, "status": event.cache_status, "outcome": event.outcome, "fetched_at": selected.get("fetched_at") if isinstance(selected, Mapping) else None},
            "provider": {"called": event.provider_called, "outcome": event.outcome, "retryable": event.retryable},
            "result": selected.get("response") if isinstance(selected, Mapping) else None,
        }
