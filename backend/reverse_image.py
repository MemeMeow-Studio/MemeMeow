"""后端反向图片检索服务。

该模块位于 FastAPI 内部接口与供应商之间，负责任务策略校验、缓存互斥、Google Vision
和 SerpApi 适配、敏感字段脱敏以及 usage event 的事实记录。Agent 只接触这里返回的
供应商无关 JSON，不会获得供应商凭证或临时上传标识。
"""

from __future__ import annotations

import base64
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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select

from backend.config import Settings
from backend.callbacks import (
    CallbackError,
    DEFAULT_CALLBACK_REGISTRY,
    callback_input_digest,
    normalize_callback_boolean,
    validate_input_digest,
    validate_binding_task,
    validate_request_binding,
)
from backend.database import DatabaseError, DatabaseResources, ReverseImageUsageEvent, ScopeContext, Task, utcnow
from backend.operation_policy import AllowAllOperationPolicy, GrantAssociation, GrantAssociationStore, OperationPolicyError, OperationPolicyGateway, Operations, require_allowed


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CACHE_SCHEMA_VERSION = 1
EMPTY_TTL = timedelta(days=3)
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
GOOGLE_VISION_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GOOGLE_VISION_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"
GOOGLE_VISION_PROVIDER = "google_vision"
GOOGLE_VISION_ENGINE = "web_detection"
SERPAPI_PROVIDER = "serpapi"
SERPAPI_ENGINE = "google_lens"
DEFAULT_PROVIDER_CACHE_VARIANT = "default"
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
PROVIDER_RESULT_LIST_FIELDS = ("visual_matches", "exact_matches", "related_content", "products", "text_results")
GOOGLE_RESULT_LIST_FIELDS = (
    "pages_with_matching_images",
    "full_matching_images",
    "partial_matching_images",
    "visually_similar_images",
    "web_entities",
    "best_guess_labels",
)
PROVIDER_RESULT_OBJECT_FIELDS = ("knowledge_graph", "about_this_image")


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
    source_image_sha256: str | None = None
    callback_binding: object | None = None
    input_digest: str | None = None

    def normalized(self) -> "ReverseImageRequest":
        """校验文件格式和检索参数，返回不含外围空白的规范化请求。"""
        try:
            name = Path(self.filename or "image").name
        except (TypeError, ValueError) as exc:
            raise ReverseImageError("invalid_image_format", "图片格式不受支持", status_code=400) from exc
        mime_type = mimetypes.guess_type(name)[0]
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ReverseImageError("invalid_image_format", "图片格式不受支持", status_code=400)
        if not self.image or len(self.image) > MAX_UPLOAD_BYTES:
            raise ReverseImageError("image_too_large", "图片超过 20 MiB 上传限制", status_code=413)
        try:
            from PIL import Image

            with Image.open(BytesIO(self.image)) as decoded:
                if decoded.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise ValueError("unsupported")
                decoded.verify()
        except Exception as exc:  # noqa: BLE001
            raise ReverseImageError("invalid_image", "上传内容不是有效图片", status_code=400) from exc
        if not isinstance(self.search_type, str) or self.search_type.strip().lower() not in {"all", "about_this_image", "products", "exact_matches", "visual_matches"}:
            raise ReverseImageError("invalid_search_type", "检索类型无效", status_code=400)
        if not isinstance(self.language, str):
            raise ReverseImageError("invalid_request", "反向图片请求参数无效", status_code=400)
        language = self.language.strip().lower()[:32] or "zh-cn"
        if self.country is not None and not isinstance(self.country, str):
            raise ReverseImageError("invalid_request", "反向图片请求参数无效", status_code=400)
        if self.query is not None and not isinstance(self.query, str):
            raise ReverseImageError("invalid_request", "反向图片请求参数无效", status_code=400)
        country = self.country.strip().lower()[:8] if self.country and self.country.strip() else None
        query = self.query.strip()[:200] if self.query and self.query.strip() else None
        source_sha = self.source_image_sha256.strip().lower() if isinstance(self.source_image_sha256, str) and self.source_image_sha256.strip() else None
        if source_sha is not None and (len(source_sha) != 64 or any(char not in "0123456789abcdef" for char in source_sha)):
            raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401)
        try:
            auto_crop = normalize_callback_boolean(self.auto_crop, field="auto_crop")
            refresh = normalize_callback_boolean(self.refresh, field="refresh")
            input_digest = validate_input_digest(self.input_digest)
        except CallbackError as exc:
            raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401) from exc
        return ReverseImageRequest(
            image=self.image,
            filename=name,
            task_id=self.task_id.strip(),
            request_id=self.request_id.strip()[:128] if self.request_id else None,
            search_type=self.search_type.strip().lower(),
            language=language,
            country=country,
            query=query,
            auto_crop=auto_crop,
            refresh=refresh,
            source_image_sha256=source_sha,
            callback_binding=self.callback_binding,
            input_digest=input_digest,
        )

    def identity(
        self,
        image_sha256: str,
        *,
        provider: str = GOOGLE_VISION_PROVIDER,
        engine: str = GOOGLE_VISION_ENGINE,
        cache_variant: str = DEFAULT_PROVIDER_CACHE_VARIANT,
    ) -> dict[str, object]:
        """构造不包含路径、策略或密钥的稳定缓存身份。"""
        return {
            "provider": provider,
            "engine": engine,
            "cache_variant": cache_variant,
            "image_sha256": image_sha256,
            "search_type": self.search_type,
            "language": self.language,
            "country": self.country,
            "query": self.query,
            "auto_crop": self.auto_crop,
        }


@dataclass(frozen=True)
class ReverseImageProviderBinding:
    """绑定宿主注入的反向图片实现及其非秘密缓存身份。

    ``name``、``engine`` 和 ``cache_variant`` 共同区分缓存与执行事实；
    ``search`` 是一次逻辑检索使用的 callable 或提供 ``search`` 方法的对象。
    适配宿主在创建应用时构造该对象，公共服务只负责透传和调用。
    """

    name: str
    engine: str
    cache_variant: str
    search: Callable[[ReverseImageRequest], dict[str, Any]] | object

    def __post_init__(self) -> None:
        """拒绝空身份或不可调用实现，避免运行后才产生错误缓存身份。"""
        if any(not isinstance(value, str) or not value.strip() for value in (self.name, self.engine, self.cache_variant)):
            raise ValueError("reverse_image_provider_binding_invalid")
        if not callable(self.search) and not callable(getattr(self.search, "search", None)):
            raise TypeError("reverse_image_provider_binding_invalid")


def derive_controlled_crop(content: bytes, *, filename: str = "image.png") -> tuple[bytes, str]:
    """从已验证的任务源图生成固定中心方形派生图及其 SHA。

    裁剪不接受 Agent 提供的物理路径或任意坐标；调用方提交的内容必须先通过图片格式和大小校验，
    服务端只使用确定性的中心裁剪并限制输出大小。
    """
    try:
        from PIL import Image

        with Image.open(BytesIO(content)) as source:
            source.load()
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > 25_000_000:
                raise ValueError("image_dimensions_invalid")
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            cropped = source.convert("RGB").crop((left, top, left + side, top + side))
            output = BytesIO()
            cropped.save(output, format="PNG", optimize=False)
            value = output.getvalue()
    except Exception as exc:  # noqa: BLE001 - 统一隐藏解码器细节
        raise ReverseImageError("invalid_image", "上传内容不是有效图片", status_code=400) from exc
    if not value or len(value) > MAX_UPLOAD_BYTES:
        raise ReverseImageError("image_too_large", "图片超过 20 MiB 上传限制", status_code=413)
    return value, hashlib.sha256(value).hexdigest()


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
                os.fchmod(handle.fileno(), 0o600)
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
    """区分合法空结果和缺少结果结构的非法供应商响应。"""
    if not isinstance(response, Mapping):
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
    present_lists = [field for field in (*PROVIDER_RESULT_LIST_FIELDS, *GOOGLE_RESULT_LIST_FIELDS) if field in response]
    present_objects = [field for field in PROVIDER_RESULT_OBJECT_FIELDS if field in response]
    if not present_lists and not present_objects:
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
    if any(not isinstance(response[field], list) for field in present_lists):
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
    if any(not isinstance(response[field], Mapping) for field in present_objects):
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
    return not any(response[field] for field in (*present_lists, *present_objects))


def _mapping_list(value: object) -> list[dict[str, object]]:
    """校验 Google 返回的对象列表，只保留可序列化的映射项。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片服务返回了无效结果", retryable=True, status_code=503)
    return [dict(item) for item in value if isinstance(item, Mapping)]


def google_vision_credentials_configured(settings: Settings) -> bool:
    """判断服务端是否存在可供 Google ADC 使用的凭证来源，不读取或返回凭证内容。"""
    configured_path = settings.google_application_credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured_path:
        return Path(configured_path).is_file()
    cloud_sdk_config = Path(os.getenv("CLOUDSDK_CONFIG", Path.home() / ".config" / "gcloud"))
    if (cloud_sdk_config / "application_default_credentials.json").is_file():
        return True
    # 云主机元数据凭证没有本地文件；项目标识是此类部署的唯一可见配置。
    return bool(settings.google_cloud_project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT"))


def _google_project_id(configured: str | None, detected: str | None) -> str:
    """按显式配置、环境变量和 ADC 探测结果确定计费项目。"""
    project_id = configured or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT") or detected
    if not project_id:
        raise ReverseImageError("reverse_image_provider_unavailable", "Google Cloud 项目未配置", retryable=True, status_code=503)
    return project_id


def _google_image_item(item: Mapping[str, object]) -> dict[str, object]:
    """把 Google 图片候选转换为 Agent 常用的链接字段。"""
    url = item.get("url")
    if not isinstance(url, str) or not url:
        return {}
    return {"link": url, "source": url}


def _google_page_item(item: Mapping[str, object]) -> dict[str, object]:
    """把 Google 匹配网页转换为 Agent 常用的标题、链接和来源字段。"""
    result: dict[str, object] = {}
    title = item.get("pageTitle")
    url = item.get("url")
    if isinstance(title, str) and title:
        result["title"] = title
        result["source"] = title
    if isinstance(url, str) and url:
        result["link"] = url
        result.setdefault("source", url)
    return result


def _deduplicate_links(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """按链接去重，保留 Google 返回顺序。"""
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for item in items:
        link = item.get("link")
        marker = str(link) if isinstance(link, str) and link else json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _normalize_google_web_detection(web_detection: Mapping[str, object]) -> dict[str, object]:
    """将 Google Web Detection 分组整理为通用候选和 Google 专属字段。"""
    pages = _mapping_list(web_detection.get("pagesWithMatchingImages"))
    full = _mapping_list(web_detection.get("fullMatchingImages"))
    partial = _mapping_list(web_detection.get("partialMatchingImages"))
    similar = _mapping_list(web_detection.get("visuallySimilarImages"))
    entities = _mapping_list(web_detection.get("webEntities"))
    labels = _mapping_list(web_detection.get("bestGuessLabels"))

    page_matches = [_google_page_item(item) for item in pages]
    exact_matches = [_google_image_item(item) for item in full]
    partial_matches = [_google_image_item(item) for item in partial]
    similar_matches = [_google_image_item(item) for item in similar]
    visual_matches = _deduplicate_links([*page_matches, *exact_matches, *partial_matches, *similar_matches])
    normalized_entities = [
        {
            key: value
            for key, value in {
                "description": item.get("description"),
                "entity_id": item.get("entityId"),
                "score": item.get("score"),
            }.items()
            if value is not None
        }
        or dict(item)
        for item in entities
    ]
    normalized_labels = [
        {
            key: value
            for key, value in {
                "label": item.get("label"),
                "language": item.get("languageCode"),
            }.items()
            if value is not None
        }
        or dict(item)
        for item in labels
    ]
    groups = {
        "pages_with_matching_images": pages,
        "full_matching_images": full,
        "partial_matching_images": partial,
        "visually_similar_images": similar,
        "web_entities": entities,
        "best_guess_labels": labels,
    }
    return {
        "visual_matches": visual_matches,
        "exact_matches": exact_matches,
        "related_content": _deduplicate_links([*partial_matches, *similar_matches]),
        "pages_with_matching_images": page_matches,
        "full_matching_images": exact_matches,
        "partial_matching_images": partial_matches,
        "visually_similar_images": similar_matches,
        "web_entities": normalized_entities,
        "best_guess_labels": normalized_labels,
        "google_vision_web_detection": groups,
    }


class GoogleVisionWebDetectionProvider:
    """调用 Google Cloud Vision Web Detection 的服务端 provider。"""

    def __init__(self, *, project_id: str | None = None, credentials_path: Path | None = None, timeout: float = 60.0, max_results: int = 20):
        self.project_id = project_id
        self.credentials_path = credentials_path
        self.timeout = timeout
        self.max_results = max(1, min(max_results, 50))

    def _credentials(self) -> tuple[object, str]:
        """从服务端 JSON 或 ADC 读取凭证，并确定请求的计费项目。"""
        try:
            import google.auth
            from google.auth.exceptions import DefaultCredentialsError

            if self.credentials_path:
                credentials, detected_project = google.auth.load_credentials_from_file(str(self.credentials_path), scopes=[GOOGLE_VISION_SCOPE])
            else:
                credentials, detected_project = google.auth.default(scopes=[GOOGLE_VISION_SCOPE])
        except DefaultCredentialsError as exc:
            raise ReverseImageError("reverse_image_provider_unavailable", "Google Vision 凭证不可用", retryable=True, status_code=503) from exc
        except (OSError, ValueError) as exc:
            raise ReverseImageError("reverse_image_provider_unavailable", "Google Vision 凭证不可用", retryable=True, status_code=503) from exc
        return credentials, _google_project_id(self.project_id, detected_project)

    def search(self, request: ReverseImageRequest) -> dict[str, object]:
        """提交一次 Web Detection 请求并返回已整理、可缓存的结果。"""
        credentials, project_id = self._credentials()
        request_body = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(request.image).decode("ascii")},
                    "features": [{"type": "WEB_DETECTION", "maxResults": self.max_results}],
                }
            ]
        }
        try:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials)
            try:
                response = session.post(
                    GOOGLE_VISION_ANNOTATE_URL,
                    headers={"x-goog-user-project": project_id},
                    json=request_body,
                    timeout=self.timeout,
                )
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001 - 不把认证或响应细节交给 Agent
            raise ReverseImageError("reverse_image_provider_unavailable", "Google Vision 请求失败", retryable=True, status_code=503) from exc
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ReverseImageError("reverse_image_provider_invalid", "Google Vision 返回了无法解析的结果", retryable=True, status_code=503) from exc
        if not getattr(response, "ok", False):
            raise ReverseImageError("reverse_image_provider_unavailable", "Google Vision 请求失败", retryable=True, status_code=503)
        if not isinstance(payload, Mapping):
            raise ReverseImageError("reverse_image_provider_invalid", "Google Vision 返回了无效结果", retryable=True, status_code=503)
        responses = payload.get("responses")
        if not isinstance(responses, list) or len(responses) != 1 or not isinstance(responses[0], Mapping):
            raise ReverseImageError("reverse_image_provider_invalid", "Google Vision 返回了无效结果", retryable=True, status_code=503)
        response_body = responses[0]
        if response_body.get("error") is not None:
            raise ReverseImageError("reverse_image_provider_invalid", "Google Vision 返回了错误结果", retryable=True, status_code=503)
        web_detection = response_body.get("webDetection")
        if not isinstance(web_detection, Mapping):
            raise ReverseImageError("reverse_image_provider_invalid", "Google Vision 缺少 Web Detection 结果", retryable=True, status_code=503)
        return _normalize_google_web_detection(web_detection)


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
    """执行指定 scope 的任务策略、缓存互斥、供应商访问和 usage event 编排。

    local 默认值只服务于开源兼容夹具；应用请求使用 scope-bound facade。
    """

    def __init__(
        self,
        settings: Settings,
        resources: DatabaseResources,
        *,
        scope_id: str | ScopeContext = "local",
        provider: Callable[[ReverseImageRequest], dict[str, Any]] | None = None,
        provider_binding: ReverseImageProviderBinding | None = None,
        operation_policy: OperationPolicyGateway | object | None = None,
        grant_store: GrantAssociationStore | None = None,
    ):
        """创建绑定单一 scope 和单一 provider 身份的反向图片服务。"""
        if provider is not None and provider_binding is not None:
            raise ValueError("reverse_image_provider_binding_conflict")
        self.settings = settings
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        cache_root = settings.reverse_image_cache_root or settings.data_root / "reverse_image_cache" / "web_detection"
        if self.scope.scope_id != "local":
            # 非 local scope 使用数据库分配的物理 namespace，客户端不能决定缓存目录。
            blob_root = resources.blob_store_for_scope(self.scope.scope_id).root
            cache_root = Path(cache_root) / "scopes" / blob_root.parent.name
        self.cache = ReverseImageCache(cache_root)
        self._provider_factory = provider
        self._provider_binding = provider_binding
        if isinstance(operation_policy, OperationPolicyGateway):
            self.operation_policy = operation_policy
        elif operation_policy is None:
            self.operation_policy = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
        else:
            self.operation_policy = OperationPolicyGateway(operation_policy)
        self.grants = grant_store or GrantAssociationStore()

    @property
    def available(self) -> bool:
        """返回不含密钥的供应商可用状态。"""
        if self._provider_binding is not None or self._provider_factory:
            return True
        if self.settings.reverse_image_provider == SERPAPI_PROVIDER:
            return bool(self.settings.serpapi_api_key)
        return google_vision_credentials_configured(self.settings)

    @property
    def provider_name(self) -> str:
        """返回当前服务固定使用的 provider 名称。"""
        if self._provider_binding is not None:
            return self._provider_binding.name
        return self.settings.reverse_image_provider

    @property
    def provider_engine(self) -> str:
        """返回当前 provider 的缓存引擎标识。"""
        if self._provider_binding is not None:
            return self._provider_binding.engine
        return SERPAPI_ENGINE if self.provider_name == SERPAPI_PROVIDER else GOOGLE_VISION_ENGINE

    @property
    def provider_cache_variant(self) -> str:
        """返回当前 provider 的非秘密缓存变体。"""
        if self._provider_binding is not None:
            return self._provider_binding.cache_variant
        return DEFAULT_PROVIDER_CACHE_VARIANT

    def _provider(self) -> object:
        """根据服务端配置创建唯一 provider；失败不会自动切换供应商。"""
        if self._provider_binding is not None:
            return self._provider_binding.search
        if self._provider_factory is not None:
            return self._provider_factory
        if self.provider_name == SERPAPI_PROVIDER:
            return SerpApiGoogleLensProvider(str(self.settings.serpapi_api_key))
        if self.provider_name == GOOGLE_VISION_PROVIDER:
            return GoogleVisionWebDetectionProvider(
                project_id=self.settings.google_cloud_project,
                credentials_path=self.settings.google_application_credentials,
            )
        raise ReverseImageError("reverse_image_provider_invalid", "反向图片 provider 配置无效", status_code=503)

    @staticmethod
    def _locked_auto_task(task: Task | None, request: ReverseImageRequest | None = None, *, scope_id: str | None = None) -> Task:
        """在缓存键锁内重新确认任务仍可执行且策略为 auto。

        缓存锁可能等待另一个进程完成供应商调用；等待期间任务状态或持久化策略
        可能已经变化，因此不能复用锁外的快照作为授权依据。
        """
        if task is None or task.task_type != "meme_context_generation":
            raise ReverseImageError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
        if task.status != "running":
            raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
        if task.claim_generation <= 0 or not task.lease_owner or task.lease_expires_at is None or task.lease_expires_at <= utcnow():
            raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
        policy = str((task.payload or {}).get("reverse_image_policy") or "forbid")
        if policy != "auto":
            raise ReverseImageError("reverse_image_forbidden", "当前任务禁止反向图片检索", status_code=403)
        binding = getattr(request, "callback_binding", None) if request is not None else None
        if binding is not None:
            try:
                if (
                    task.id != binding.task_id
                    or (scope_id is not None and task.scope_id != scope_id)
                    or task.scope_id != binding.scope_id
                    or task.claim_generation != binding.claim_generation
                    or task.lease_owner != binding.owner
                    or task.attempt_count != binding.attempt
                    or not binding.allows("analysis.reverse_image_search")
                ):
                    raise ValueError("callback_execution_mismatch")
            except Exception as exc:  # noqa: BLE001 - callback 失败不得暴露任务状态
                raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401) from exc
        return task

    @staticmethod
    def _reconcile_callback_from_usage(environment: Any, callback_row: Any, event: ReverseImageUsageEvent) -> None:
        """按已经持久化的 usage 终态收束 callback 事实。

        callback 记录和 usage 记录不在同一张表，进程可能在 usage 完成后、callback
        终态写回前退出。只有 usage 已明确得到成功、空结果或失败时才能把 callback
        从 ``started``/``unknown_execution`` 收束；provider 仍在途时必须保留未知语义。
        """
        if callback_row is None or event.completed_at is None:
            return
        known_success = event.outcome in {"success", "empty"}
        error_code = (event.error or {}).get("error") if isinstance(event.error, Mapping) else None
        if error_code == "reverse_image_unknown_execution":
            if callback_row.state != "unknown_execution":
                environment.callback_requests.finish(
                    callback_row.request_id,
                    state="unknown_execution",
                    error={"error": "reverse_image_unknown_execution"},
                )
            raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
        known_failure = event.outcome in {"failed", "forbidden"}
        if not known_success and not known_failure:
            return
        if known_success:
            if callback_row.state == "failed":
                raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
            if callback_row.state in {"started", "unknown_execution"}:
                environment.callback_requests.finish(
                    callback_row.request_id,
                    state="completed",
                    result={"outcome": event.outcome},
                )
            return
        if callback_row.state == "completed":
            raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
        if callback_row.state in {"started", "unknown_execution"}:
            environment.callback_requests.finish(
                callback_row.request_id,
                state="failed",
                error={"error": error_code if isinstance(error_code, str) else "reverse_image_failed"},
            )

    def search(self, request: ReverseImageRequest) -> dict[str, object]:
        """按 task_id 校验运行任务并执行一次供应商无关逻辑检索。"""
        request = request.normalized()
        image_sha = hashlib.sha256(request.image).hexdigest()
        provider_identity = {
            "provider": self.provider_name,
            "engine": self.provider_engine,
            "cache_variant": self.provider_cache_variant,
        }
        key = _fingerprint(request.identity(image_sha, **provider_identity))
        binding = request.callback_binding
        request_id = request.request_id
        callback_row = None
        with self.resources.environment(self.scope.scope_id) as environment:
            task = environment.tasks.get(request.task_id, for_update=True)
            if task is None or task.task_type != "meme_context_generation":
                if binding is not None:
                    raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401)
                raise ReverseImageError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
            if task.status != "running" or task.claim_generation <= 0 or not task.lease_owner or task.lease_expires_at is None or task.lease_expires_at <= utcnow():
                if binding is not None:
                    raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401)
                raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
            if binding is not None:
                try:
                    registration = DEFAULT_CALLBACK_REGISTRY.get("/internal/reverse-image/search")
                    validate_binding_task(binding, task, registration)
                except (CallbackError, ValueError, TypeError) as exc:
                    raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401) from exc
            task_meme_id = (task.payload or {}).get("meme_id")
            target_record = environment.memes.get(task_meme_id) if isinstance(task_meme_id, str) else None
            if isinstance(task_meme_id, str) and target_record is None:
                if binding is not None:
                    raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401)
                raise ReverseImageError("task_not_running", "任务当前不可执行反向图片检索", status_code=409)
            if binding is not None:
                # target_sha256 仍是旧 token 的兼容声明，但不再限制 Agent 只能提交任务原图。
                try:
                    server_digest = callback_input_digest(
                        scope_id=self.scope.scope_id,
                        task_id=task.id,
                        claim_generation=binding.claim_generation,
                        attempt=binding.attempt,
                        operation="analysis.reverse_image_search",
                        target_sha256=binding.target_sha256,
                        image_sha256=image_sha,
                        search_type=request.search_type,
                        language=request.language,
                        country=request.country,
                        query=request.query,
                        auto_crop=request.auto_crop,
                        refresh=request.refresh,
                    )
                    request_id, _client_digest = validate_request_binding(
                        request_id,
                        binding,
                        input_digest=request.input_digest,
                        computed_input_digest=server_digest,
                    )
                except CallbackError as exc:
                    raise ReverseImageError("agent_callback_invalid_execution", "内部执行绑定无效", status_code=401) from exc
                request = replace(request, input_digest=server_digest)
                task_authority = environment.callback_requests.get_by_task_operation(
                    task_id=task.id,
                    operation="analysis.reverse_image_search",
                    for_update=True,
                )
                if task_authority is not None and task_authority.input_digest != server_digest:
                    raise ReverseImageError(
                        "reverse_image_call_limit_reached",
                        "当前语境已使用反向图片调用额度",
                        status_code=409,
                    )
                try:
                    callback_row = environment.callback_requests.resolve(
                        request_id=request_id,
                        task_id=task.id,
                        claim_generation=binding.claim_generation,
                        attempt=binding.attempt,
                        operation="analysis.reverse_image_search",
                        target_sha256=binding.target_sha256,
                        input_digest=server_digest,
                    )
                except DatabaseError as exc:
                    if exc.code in {"callback_request_conflict", "callback_binding_conflict"}:
                        raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409) from exc
                    raise ReverseImageError("agent_callback_unavailable", "内部执行绑定暂不可用", status_code=503) from exc
                request_id = callback_row.request_id
                if task_authority is None:
                    # 当前 Task 行已锁定；首条 callback authority 提交后再释放事务，
                    # 后续图片处理和 provider 网络请求不持有数据库连接。
                    environment.uow.session.flush()
                    environment.uow.session.commit()
                try:
                    bound_usage = environment.reverse_image_usage.get_by_binding(
                        task_id=task.id,
                        claim_generation=binding.claim_generation,
                        attempt=binding.attempt,
                        operation="analysis.reverse_image_search",
                        target_sha256=binding.target_sha256,
                        input_digest=server_digest,
                        for_update=True,
                    )
                except DatabaseError as exc:
                    if exc.code == "usage_request_conflict":
                        raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409) from exc
                    raise ReverseImageError("agent_callback_unavailable", "内部执行绑定暂不可用", status_code=503) from exc
                if bound_usage is not None and bound_usage.request_id != request_id:
                    # callback 行缺失时，完整 usage 事实可能来自旧崩溃窗口；其 ID
                    # 无法安全改绑到新候选，避免通过新 ID 重放 provider。
                    raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
                if callback_row.state in {"unknown_execution", "failed", "completed"} and callback_row.completed_at is not None:
                    # callback 终态而 usage 缺失表示崩溃窗口无法证明完整结果；只允许
                    # 返回已存在的 usage，不能用新 request ID 再次触发 provider。
                    existing = environment.reverse_image_usage.get(request_id, for_update=True)
                    if existing is None:
                        raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
            if request_id is None:
                request_id = secrets.token_urlsafe(24)
            # 先恢复同一权威 request 的 usage；逻辑 request resolver 已在此前完成，
            # 因而换 ID 的重试不会到达第二条 usage/provider/grant 路径。
            existing = environment.reverse_image_usage.get(request_id, for_update=True)
            if existing is not None:
                if existing.task_id != request.task_id or existing.cache_key != key or (
                    binding is not None
                    and (
                        existing.claim_generation != binding.claim_generation
                        or existing.attempt != binding.attempt
                        or existing.operation != "analysis.reverse_image_search"
                        or existing.target_sha256 != binding.target_sha256
                        or existing.input_digest != request.input_digest
                    )
                ):
                    raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409)
                if existing.completed_at is not None:
                    self._reconcile_callback_from_usage(environment, callback_row, existing)
                    return self._event_output(existing)
                if binding is not None and (
                    existing.provider_called
                    or (callback_row is not None and callback_row.state in {"failed", "completed", "unknown_execution"})
                ):
                    # provider 已开始，或 callback 已有不可与未完成 usage 证明一致的
                    # 终态时，只能返回稳定未知状态，不能重新 acquire 或联系 provider。
                    if callback_row is not None and callback_row.completed_at is None:
                        environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
                    environment.uow.session.commit()
                    raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
                if callback_row is not None and existing.provider_called and callback_row.completed_at is None:
                    environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
                return self._event_output(existing)
            policy = str((task.payload or {}).get("reverse_image_policy") or "forbid")
            if policy != "auto":
                event = environment.reverse_image_usage.create(request_id=request_id, task_id=task.id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="miss", **self._usage_binding(request))
                environment.reverse_image_usage.finish(event.request_id, outcome="forbidden", result={"used": False})
                if callback_row is not None:
                    environment.callback_requests.finish(request_id, state="failed", result={"used": False}, error={"error": "reverse_image_forbidden"})
                # 异常响应会触发 UOW 回滚；先提交禁止请求的审计记录，确保拒绝也可追溯。
                environment.uow.session.commit()
                raise ReverseImageError("reverse_image_forbidden", "当前任务禁止反向图片检索", status_code=403)

        timestamp = datetime.now(UTC)
        with self.cache.lock(key):
            record = self.cache.load(key)
            snapshot = _latest(record)
            if not request.refresh and _reusable(snapshot, timestamp):
                with self.resources.environment(self.scope.scope_id) as environment:
                    task = self._locked_auto_task(environment.tasks.get(request.task_id), request, scope_id=self.scope.scope_id)
                    event = environment.reverse_image_usage.create(request_id=request_id, task_id=request.task_id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="hit", **self._usage_binding(request))
                    event = environment.reverse_image_usage.finish(
                        event.request_id,
                        cache_status="hit",
                        outcome=str(snapshot.get("outcome") or "empty"),
                        result={"used": bool(snapshot and snapshot.get("outcome") == "success"), "snapshot": snapshot},
                    )
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="completed", result={"cache_status": "hit"})
                    return self._event_output(event)
            with self.resources.environment(self.scope.scope_id) as environment:
                task = self._locked_auto_task(environment.tasks.get(request.task_id), request, scope_id=self.scope.scope_id)
                event = environment.reverse_image_usage.create(request_id=request_id, task_id=request.task_id, meme_id=(task.payload or {}).get("meme_id"), cache_key=key, cache_status="refresh" if record else "miss", provider=self.provider_name, **self._usage_binding(request))
                if event.task_id != request.task_id or event.cache_key != key:
                    raise ReverseImageError("usage_request_conflict", "请求标识已用于另一项检索", status_code=409)
                if event.completed_at is not None:
                    return self._event_output(event)
                if event.provider_called:
                    # 进程可能在供应商调用后中断；未知结果只保留已计数状态，不自动重放付费请求。
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
                    return self._event_output(event)
                if not self.available:
                    environment.reverse_image_usage.finish(request_id, outcome="failed", retryable=True, error={"error": "reverse_image_unavailable"})
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="failed", error={"error": "reverse_image_unavailable"})
                    environment.uow.session.commit()
                    raise ReverseImageError("reverse_image_unavailable", "反向图片服务尚未配置", retryable=True, status_code=503)
                operation_request = self.operation_policy.request(
                    self.scope,
                    Operations.ANALYSIS_REVERSE_IMAGE_SEARCH,
                    f"reverse:{request_id}",
                    # 任务允许没有关联 Meme 的历史/夹具记录；缺失资源必须保持
                    # ``None``，不能把空字符串伪装成可信资源标识交给 policy。
                    resource_id=str((task.payload or {}).get("meme_id")) if (task.payload or {}).get("meme_id") else None,
                    task_id=task.id,
                    source="reverse-image-provider",
                    input_digest=request.input_digest,
                )
                try:
                    association = self.grants.get(operation_request)
                    if association is None:
                        if hasattr(self.grants, "acquire"):
                            association = self.grants.acquire(operation_request, self.operation_policy)
                        else:
                            grant = require_allowed(self.operation_policy.acquire(operation_request))
                            association = self.grants.put(GrantAssociation(operation_request, grant))
                except OperationPolicyError as exc:
                    event = environment.reverse_image_usage.create(
                        request_id=request_id,
                        task_id=request.task_id,
                        meme_id=(task.payload or {}).get("meme_id"),
                        cache_key=key,
                        cache_status="refresh" if record else "miss",
                        provider=self.provider_name,
                        **self._usage_binding(request),
                    )
                    event = environment.reverse_image_usage.finish(
                        request_id,
                        outcome="forbidden",
                        result={"used": False, "degraded": True, "reason": exc.code},
                        error={"error": exc.code},
                    )
                    if callback_row is not None:
                        environment.callback_requests.finish(request_id, state="failed", result={"used": False}, error={"error": exc.code})
                    return self._event_output(event)
                if association.state in {"committed", "released", "unknown"}:
                    # 已计量或结果未知的逻辑 request 不得再次联系 provider。
                    event = environment.reverse_image_usage.finish(
                        request_id,
                        outcome="failed",
                        error={"error": "reverse_image_unknown_execution"},
                    )
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
                        environment.uow.session.commit()
                        raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503)
                    return self._event_output(event)
                try:
                    commit_result = self.operation_policy.commit(association.grant)
                    if not commit_result.ok or commit_result.state not in {"committed", "already_committed"}:
                        raise OperationPolicyError(commit_result.reason or "operation_policy_unavailable", retry_at=commit_result.retry_at)
                    transition = getattr(self.grants, "transition", None)
                    if not callable(transition) or not transition(association.grant, "committed"):
                        raise OperationPolicyError("operation_grant_invalid")
                    # commit 成功后再持久化 provider_started，避免策略拒绝被误记为
                    # 已经联系供应商；该事实提交后才离开缓存锁调用 provider。
                    environment.reverse_image_usage.mark_provider_started(event.request_id)
                except (OperationPolicyError, DatabaseError) as exc:
                    # provider 尚未启动但计量或审计状态无法确认，保留 unknown 事实并禁止重放。
                    transition = getattr(self.grants, "transition", None)
                    if callable(transition):
                        try:
                            transition(association.grant, "unknown")
                        except OperationPolicyError:
                            pass
                    environment.reverse_image_usage.finish(request_id, outcome="failed", error={"error": "reverse_image_unknown_execution"})
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
                    raise ReverseImageError("reverse_image_unknown_execution", "反向图片调用状态未知", status_code=503) from exc
                if binding is not None:
                    environment.callback_requests.finish(request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
            try:
                provider = self._provider()
                response = provider(request) if callable(provider) else provider.search(request)
                outcome = "empty" if _is_empty(response) else "success"
                snapshot = {"fetched_at": timestamp.isoformat(), "outcome": outcome, "expires_at": (timestamp + EMPTY_TTL).isoformat() if outcome == "empty" else None, "response": sanitize_value(response)}
                next_record = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    **provider_identity,
                    "request": request.identity(image_sha, **provider_identity),
                    "snapshots": [*(record or {}).get("snapshots", []), snapshot],
                }
                self.cache.write(key, next_record)
            except ReverseImageError as exc:
                with self.resources.environment(self.scope.scope_id) as environment:
                    event = environment.reverse_image_usage.finish(request_id, outcome="failed", retryable=exc.retryable, error={"error": exc.code})
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="failed", error={"error": exc.code})
                raise
            except Exception as exc:  # noqa: BLE001
                # 供应商适配器异常也必须收束 started 事件，避免永久悬挂且不回显原始正文。
                error = ReverseImageError("reverse_image_provider_unavailable", "反向图片服务暂时不可用", retryable=True, status_code=503)
                with self.resources.environment(self.scope.scope_id) as environment:
                    environment.reverse_image_usage.finish(request_id, outcome="failed", retryable=True, error={"error": error.code})
                    if binding is not None:
                        environment.callback_requests.finish(request_id, state="failed", error={"error": error.code})
                raise error from exc
            with self.resources.environment(self.scope.scope_id) as environment:
                event = environment.reverse_image_usage.finish(request_id, cache_status="refresh" if record else "miss", outcome=outcome, result={"used": outcome == "success", "snapshot": snapshot})
                if binding is not None:
                    environment.callback_requests.finish(request_id, state="completed", result={"outcome": outcome})
                return self._event_output(event, snapshot=snapshot)

    @staticmethod
    def _event_output(event: ReverseImageUsageEvent, *, snapshot: Mapping[str, object] | None = None) -> dict[str, object]:
        """将事件映射为稳定供应商无关 JSON。"""
        payload = event.result or {}
        selected = snapshot or payload.get("snapshot")
        result = selected.get("response") if isinstance(selected, Mapping) else None
        output: dict[str, object] = {
            "request_id": event.request_id,
            "cache": {"key": event.cache_key, "status": event.cache_status, "outcome": event.outcome, "fetched_at": selected.get("fetched_at") if isinstance(selected, Mapping) else None},
            "provider": {"called": event.provider_called, "outcome": event.outcome, "retryable": event.retryable},
            "result": result,
        }
        if isinstance(payload.get("degraded"), bool):
            output["degraded"] = payload["degraded"]
        if isinstance(payload.get("reason"), str):
            output["reason"] = payload["reason"]
        return output

    @staticmethod
    def _usage_binding(request: ReverseImageRequest) -> dict[str, object]:
        """提取已由 callback 边界验证的执行绑定，普通直连请求保持兼容。"""
        binding = request.callback_binding
        if binding is None:
            return {}
        return {
            "claim_generation": binding.claim_generation,
            "attempt": binding.attempt,
            "operation": "analysis.reverse_image_search",
            "target_sha256": binding.target_sha256,
            "input_digest": request.input_digest,
        }
