"""公网结果与任务状态的最小 DTO 边界。

本模块位于 Agent 结果接收和任务查询之间，集中执行不可信文本的敏感数据检查、
历史任务结果清理以及图片处理状态的显式字段投影。它不负责授权或 scope 查询；
调用方必须先完成对应的业务绑定，再使用这里的 DTO。
"""

from __future__ import annotations

import base64
import ipaddress
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit


class PublicDataError(ValueError):
    """不可信结果不符合公开数据边界。异常消息不携带原始输入。"""

    def __init__(self, code: str) -> None:
        """保存稳定原因码，供调用方记录诊断而不暴露原始文本。"""
        self.code = code
        super().__init__(code)


AGENT_RESULT_FIELDS = frozenset(
    {
        "title",
        "summary",
        "subjects",
        "visible_text",
        "references",
        "meaning",
        "keywords",
        "search_queries",
        "uncertainties",
        "source_urls",
    }
)
AGENT_RESULT_REQUIRED_FIELDS = AGENT_RESULT_FIELDS - {"source_urls"}
PUBLIC_STAGE_NAMES = frozenset({"visual", "agent", "auto_rename", "text_embedding"})
PUBLIC_STAGE_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "blocked", "unknown_execution", "skipped", "warning"})
PUBLIC_TASK_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "blocked", "unknown_execution"})
# 公开摘要只接受当前 snapshot 协议和服务端候选上限，避免历史/伪造 JSON
# 被误当成可恢复的视觉事实。
VISUAL_SNAPSHOT_PROTOCOL_VERSION = 2
VISUAL_SNAPSHOT_MAX_CANDIDATES = 50

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_SECRET_NAME_RE = re.compile(r"(?i)(?:secret|token|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)")
_INTERNAL_NAME_RE = re.compile(
    r"(?i)(?:scope(?:[_-]?id)?|workspace|task(?:[_-]?id)?|attempt(?:[_-]?id)?|session(?:[_-]?id)?|"
    r"executor|callback|provider|account(?:[_-]?id)?|user(?:[_-]?id)?|subscription|plan(?:[_-]?id)?|"
    r"billing|quota|grant|traceback|stack)"
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s\"'(<=:])(?:/|~/|[A-Za-z]:[\\/]|\\\\)(?=[^\s\"'<>)]|$)[^\s\"'<>)]*"
)
_KNOWN_PATH_RE = re.compile(
    r"(?i)(?:^|[\s\"'(<=:])/(?:runtime|images|skills|app|home|tmp|var|etc|proc|sys|mnt|workspace)"
    r"(?:[/\\]|$|[.,;!?，。！？；：)\]}])"
)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
# 公开文本常用 ``A /B`` 或 ``A / B`` 表示并列；只屏蔽一个词的分隔符，
# 多级路径、已知运行目录和赋值/冒号形式仍交给绝对路径规则拒绝。
_NATURAL_SLASH_SEPARATOR_RE = re.compile(
    r"(?<=\s)/(?!"
    r"(?:runtime|images|skills|app|home|tmp|var|etc|proc|sys|mnt|workspace)"
    r"(?=$|\s|[/\\.,;!?，。！？；：)\]}])"
    r")[^\s/\\\"'<>()[\]{}?,#，。！？；：、]+"
    r"(?=\s|$|[,，。!?;：)\]}])",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b")
_SENSITIVE_ASSIGNMENT_RE = re.compile(r"(?i)\b(?:secret|token|password|passwd|api[_-]?key|authorization|cookie|credential)\s*[:=]\s*[^\s,;]+")
_CREDENTIAL_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs]-)[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_INTERNAL_QUERY_KEYS = frozenset(
    {
        "account_id",
        "attempt_id",
        "executor_attempt_id",
        "operation_grant",
        "plan_id",
        "scope_id",
        "session_id",
        "subscription_id",
        "task_id",
        "user_id",
        "workspace_selector",
    }
)
_INTERNAL_VALUE_RE = re.compile(
    r"(?i)(?:^|[\s:/._=-])(?:/internal(?:/|$)|scope[_-]?id|workspace[_-]?selector|task[_-]?id|"
    r"attempt[_-]?id|session[_-]?id|executor[_-]?attempt|account[_-]?id|user[_-]?id|"
    r"subscription[_-]?id|plan[_-]?id|billing|quota|operation[_-]?grant)(?:$|[\s:/._=-])"
)


def _secret_variants(value: str) -> set[str]:
    """生成登记凭据的少量常见编码变体，避免把原值写入诊断。"""
    variants = {value}
    decoded = value
    for _ in range(2):
        decoded = unquote(decoded)
        variants.add(decoded)
    if len(value.encode("utf-8")) >= 8:
        raw = value.encode("utf-8")
        variants.add(base64.b64encode(raw).decode("ascii"))
        variants.add(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    return {item for item in variants if len(item) >= 8}


def secret_inventory_from_mapping(values: Mapping[str, object] | None) -> tuple[str, ...]:
    """从配置映射提取凭据值；返回值只供内存中的匹配使用。"""
    inventory: set[str] = set()
    for name, value in (values or {}).items():
        if not _SECRET_NAME_RE.search(str(name)) or not isinstance(value, str) or not value.strip():
            continue
        inventory.update(_secret_variants(value.strip()))
    return tuple(sorted(inventory, key=lambda item: (-len(item), item)))


def secret_inventory_from_settings(settings: object) -> tuple[str, ...]:
    """从 Settings 的已加载字段提取凭据，绝不序列化或记录配置原值。"""
    values: Mapping[str, object] | None = None
    dump = getattr(settings, "model_dump", None)
    if callable(dump):
        try:
            candidate = dump()
            if isinstance(candidate, Mapping):
                values = candidate
        except Exception:  # noqa: BLE001 - 安全扫描不能影响正常错误收束
            values = None
    if values is None:
        candidate = getattr(settings, "__dict__", {})
        values = candidate if isinstance(candidate, Mapping) else {}
    return secret_inventory_from_mapping(values)


def _urls(value: str) -> Iterable[str]:
    """提取字符串中的 URI，供 userinfo、内部目标和本地协议检查。"""
    for match in _URL_RE.finditer(value):
        yield match.group(0).rstrip(".,;)]}")


def _without_urls(value: str) -> str:
    """用空格屏蔽 URI 区段，避免 URI 查询参数触发路径文本扫描。"""
    return _URL_RE.sub(" ", value)


def _strip_natural_slash_separators(value: str) -> str:
    """屏蔽不带路径特征的自然语言斜杠，并保留赋值/冒号路径供后续检测。"""

    def replace(match: re.Match[str]) -> str:
        prefix = value[: match.start()]
        # ``path = /foo`` 和 ``file: /foo`` 即使有空格也属于显式路径形式。
        if re.search(r"[=:]\s*$", prefix):
            return match.group(0)
        return " "

    return _NATURAL_SLASH_SEPARATOR_RE.sub(replace, value)


def _transform_outside_urls(value: str, transform: Callable[[str], str]) -> str:
    """只对 URI 之外的文本执行转换，避免查询参数被当作本地路径。"""
    parts: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(value):
        parts.append(transform(value[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(transform(value[cursor:]))
    return "".join(parts)


def _replace_outside_urls(value: str, pattern: re.Pattern[str], replacement: str) -> str:
    """仅在 URI 之外替换诊断文本中的路径，保留后续 URI 脱敏步骤。"""
    return _transform_outside_urls(value, lambda segment: pattern.sub(replacement, segment))


def _unsafe_url(value: str) -> bool:
    """判断 URL 是否包含认证信息、敏感查询参数或内部网络目标。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.scheme.lower() not in {"http", "https"}:
        return True
    if not parsed.netloc:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    normalized_query_keys = {key.lower().replace("-", "_") for key, _ in query_pairs}
    if normalized_query_keys & _SENSITIVE_QUERY_KEYS or normalized_query_keys & _INTERNAL_QUERY_KEYS:
        return True
    try:
        hostname = parsed.hostname
        if hostname:
            address = ipaddress.ip_address(hostname)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified:
                return True
    except ValueError:
        # 域名不做 DNS 解析；已登记的内部命名仍通过固定标识检查拒绝。
        pass
    hostname = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    return hostname in {"localhost", "metadata.google.internal"} or hostname.endswith((".local", ".internal")) or path == "/internal" or path.startswith("/internal/")


def _unsafe_text(value: str, *, secret_inventory: tuple[str, ...]) -> bool:
    """检查单个公开文本值，不修改输入内容。"""
    if any(secret and secret in value for secret in secret_inventory):
        return True
    if _BEARER_RE.search(value) or _SENSITIVE_ASSIGNMENT_RE.search(value) or any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS):
        return True
    urls = tuple(_urls(value))
    if any(_unsafe_url(url) or _INTERNAL_VALUE_RE.search(url) for url in urls):
        return True
    text = _without_urls(value)
    if _INTERNAL_VALUE_RE.search(text):
        return True
    text = _strip_natural_slash_separators(text)
    if _ABSOLUTE_PATH_RE.search(text) or _KNOWN_PATH_RE.search(text):
        return True
    if _INTERNAL_VALUE_RE.search(text):
        return True
    return False


def _walk_untrusted(value: object, *, secret_inventory: tuple[str, ...], top_level: bool = False) -> bool:
    """递归检查结果值和嵌套键，阻止敏感字段藏在扩展结构中。"""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                return True
            if not top_level and (_SECRET_NAME_RE.search(key) or _INTERNAL_NAME_RE.search(key)):
                return True
            if _walk_untrusted(child, secret_inventory=secret_inventory):
                return True
        return False
    if isinstance(value, list):
        return any(_walk_untrusted(item, secret_inventory=secret_inventory) for item in value)
    if isinstance(value, str):
        return _unsafe_text(value, secret_inventory=secret_inventory)
    return value is not None and not isinstance(value, (bool, int, float))


def scan_public_result(value: object, *, secret_inventory: Iterable[str] = ()) -> str | None:
    """扫描公开结果的敏感数据边界并返回稳定原因码。

    输入为待交付的 JSON 值；返回 ``None`` 表示未发现敏感内容。该轻量扫描供
    Agent 预检和服务端最终校验共用，不负责顶层字段、JSON Schema 或领域模型校验。
    """
    if not isinstance(value, Mapping):
        return "result_object_required"
    inventory = tuple(item for item in secret_inventory if isinstance(item, str) and len(item) >= 8)
    return "result_sensitive_data" if _walk_untrusted(dict(value), secret_inventory=inventory, top_level=True) else None


def validate_agent_result(value: object, *, secret_inventory: Iterable[str] = ()) -> dict[str, Any]:
    """在 Agent 结果接收边界验证顶层字段和敏感数据，命中即整体拒绝。"""
    if not isinstance(value, Mapping):
        raise PublicDataError("result_object_required")
    unknown = set(value) - AGENT_RESULT_FIELDS
    if unknown or not AGENT_RESULT_REQUIRED_FIELDS.issubset(value):
        raise PublicDataError("result_schema_invalid")
    reason_code = scan_public_result(value, secret_inventory=secret_inventory)
    if reason_code:
        raise PublicDataError(reason_code)
    return dict(value)


def normalize_public_identifier(value: object) -> str | None:
    """返回可放入公开 DTO 的 opaque 标识，路径和控制字符会被拒绝。"""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        return None
    return value


def normalize_public_code(value: object, *, fallback: str | None = None) -> str | None:
    """收窄公开错误码、状态码和配置枚举，避免旧 JSON 原样回显。"""
    if isinstance(value, str) and _CODE_RE.fullmatch(value):
        return value
    return fallback if isinstance(fallback, str) and _CODE_RE.fullmatch(fallback) else None


def normalize_public_digest(value: object) -> str | None:
    """只保留公开图片指纹的固定十六进制格式。"""
    if isinstance(value, str) and _SHA256_RE.fullmatch(value):
        return value.lower()
    return None


def sanitize_visual_snapshot_summary(value: object) -> dict[str, Any] | None:
    """将视觉 snapshot 投影为固定 protocol v2 的公开摘要。"""
    if not isinstance(value, Mapping):
        return None
    version = value.get("protocol_version")
    candidate_count = value.get("candidate_count")
    snapshot_hash = normalize_public_digest(value.get("snapshot_sha256"))
    matched_at = sanitize_public_timestamp(value.get("matched_at"))
    if (
        version != VISUAL_SNAPSHOT_PROTOCOL_VERSION
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or not 0 <= candidate_count <= VISUAL_SNAPSHOT_MAX_CANDIDATES
        or snapshot_hash is None
        or matched_at is None
    ):
        return None
    return {
        "protocol_version": VISUAL_SNAPSHOT_PROTOCOL_VERSION,
        "snapshot_sha256": snapshot_hash,
        "matched_at": matched_at,
        "candidate_count": candidate_count,
    }


def sanitize_public_timestamp(value: object) -> str | None:
    """保留可解析的 ISO 时间，拒绝路径、换行和异常正文。"""
    if isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or not value or len(value) > 64 or any(ord(char) < 32 for char in value):
        return None
    candidate = value.strip()
    if not candidate or ("T" not in candidate and " " not in candidate):
        return None
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate


def sanitize_public_message(value: object, *, fallback: str | None = None) -> str | None:
    """将历史消息压缩为单行低敏感文本，不保留 URL、路径或凭据。"""
    if not isinstance(value, str):
        return fallback
    message = value.splitlines()[0].strip() if value.splitlines() else ""
    message = _BEARER_RE.sub("[REDACTED]", message)
    for pattern in _CREDENTIAL_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    message = _transform_outside_urls(message, _strip_natural_slash_separators)
    message = _replace_outside_urls(message, _ABSOLUTE_PATH_RE, "[PATH]")
    message = _replace_outside_urls(message, _KNOWN_PATH_RE, "[PATH]")
    message = _URL_RE.sub("[URL]", message)
    message = re.sub(r"(?i)(?:secret|token|password|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+", "[REDACTED]", message)
    message = re.sub(
        r"(?i)(?:scope(?:[_-]?id)?|workspace(?:[_-]?selector)?|task(?:[_-]?id)?|attempt(?:[_-]?id)?|session(?:[_-]?id)?|executor(?:[_-]?attempt)?|billing|quota)\s*[:=]\s*[^\s,;]+",
        "[REDACTED]",
        message,
    )
    return message[:500] or fallback


def _safe_filename(value: object) -> str | None:
    """保留不含目录的公开文件名，避免历史绝对路径进入响应。"""
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or ":" in value
        or value.startswith("~")
        or value in {".", ".."}
        or any(ord(char) < 32 for char in value)
        or _unsafe_text(value, secret_inventory=())
    ):
        return None
    return value[:255]


def normalize_public_filename(value: object) -> str | None:
    """返回不含目录分隔符的公开文件名，供任务摘要和媒体元数据复用。"""
    return _safe_filename(value)


def _safe_int(value: object, *, minimum: int = 0, maximum: int = 10_000_000) -> int | None:
    """保留有限整数结果字段。"""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        return None
    return value


def _safe_scalar(value: object) -> str | int | bool | None:
    """保留不含路径和控制字符的简单公开标量。"""
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if not isinstance(value, str) or not value or len(value) > 255 or any(ord(char) < 32 for char in value):
        return None
    if _unsafe_text(value, secret_inventory=()):
        return None
    return value


def _safe_relative_names(value: object) -> list[str] | None:
    """清理完整性报告中的相对文件名列表，不保留宿主路径。"""
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value[:500]:
        if (
            not isinstance(item, str)
            or not item
            or item.startswith(("/", "\\", "~"))
            or "\\" in item
            or any(part.endswith(":") for part in item.split("/"))
            or any(ord(char) < 32 for char in item)
            or _unsafe_text(item, secret_inventory=())
        ):
            continue
        parts = item.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            continue
        result.append(item[:255])
    return result


def sanitize_task_result(task_type: object, value: object) -> dict[str, Any] | None:
    """按任务类型投影结果字段，历史未知结构一律省略。"""
    if not isinstance(task_type, str) or not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    if task_type == "meme_context_generation":
        audit = value.get("reverse_image")
        if isinstance(audit, Mapping):
            public_audit: dict[str, Any] = {}
            policy = audit.get("policy")
            if isinstance(policy, str) and policy in {"forbid", "auto"}:
                public_audit["policy"] = policy
            for key in ("attempted", "used"):
                if isinstance(audit.get(key), bool):
                    public_audit[key] = audit[key]
            for key in ("cache_hits", "provider_calls", "request_count"):
                safe = _safe_int(audit.get(key))
                if safe is not None:
                    public_audit[key] = safe
            outcome = normalize_public_code(audit.get("outcome"))
            if outcome:
                public_audit["outcome"] = outcome
            if public_audit:
                result["reverse_image"] = public_audit
        snapshot = sanitize_visual_snapshot_summary(value.get("visual_match_snapshot"))
        if snapshot is not None:
            result["visual_match_snapshot"] = snapshot
    elif task_type == "visual_embedding_generation":
        for key in ("visual_model", "preprocess_version"):
            scalar = _safe_scalar(value.get(key))
            if isinstance(scalar, str):
                result[key] = scalar
        dimensions = _safe_int(value.get("dimensions"), maximum=100_000)
        if dimensions is not None:
            result["dimensions"] = dimensions
    elif task_type == "image_auto_rename":
        filename = _safe_filename(value.get("saved_filename"))
        if filename is not None:
            result["saved_filename"] = filename
        if isinstance(value.get("auto_named"), bool):
            result["auto_named"] = value["auto_named"]
    elif task_type == "text_embedding_generation":
        model = _safe_scalar(value.get("embedding_model"))
        if isinstance(model, str):
            result["embedding_model"] = model
    elif task_type == "cache_generation":
        for key in ("indexed_count", "skipped_count"):
            safe = _safe_int(value.get(key))
            if safe is not None:
                result[key] = safe
        model = _safe_scalar(value.get("model"))
        if isinstance(model, str):
            result["model"] = model
        dimensions = _safe_int(value.get("dimensions"), maximum=100_000)
        if dimensions is not None:
            result["dimensions"] = dimensions
    elif task_type == "metadata_repair":
        for key in ("processed", "created", "repaired", "repair_required"):
            safe = _safe_int(value.get(key))
            if safe is not None:
                result[key] = safe
        for key in ("orphan_files", "missing_files", "mismatched"):
            names = _safe_relative_names(value.get(key))
            if names is not None:
                result[key] = names
    return result or None


def sanitize_public_error(value: object, *, fallback: str = "task_failed") -> dict[str, Any] | None:
    """将任务或阶段错误转换为稳定的公开错误 DTO。"""
    if not isinstance(value, Mapping):
        return None
    safe_fallback = normalize_public_code(fallback) or "task_failed"
    code = normalize_public_code(value.get("error"), fallback=safe_fallback) or safe_fallback
    message = sanitize_public_message(value.get("message"), fallback=code) or code
    result: dict[str, Any] = {"error": code, "message": message}
    status = value.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    return result


def public_processing_stage(value: Mapping[str, object], *, job_id: str | None = None) -> dict[str, object]:
    """为图片处理阶段建立显式公开字段集合。"""
    if not isinstance(value, Mapping):
        return {"stage": "unknown", "status": "failed"}
    raw_stage = value.get("stage")
    stage = raw_stage if isinstance(raw_stage, str) and raw_stage in PUBLIC_STAGE_NAMES else None
    raw_status = value.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in PUBLIC_STAGE_STATUSES else "failed"
    result: dict[str, object] = {"stage": stage or "unknown", "status": status}
    for key in ("task_id", "session_id", "executor_attempt_id"):
        identifier = normalize_public_identifier(value.get(key))
        if identifier:
            result[key] = identifier
    attempt = _safe_int(value.get("attempt"), maximum=1_000_000)
    if attempt is not None:
        result["attempt"] = attempt
    error = sanitize_public_error(value.get("error"), fallback="image_processing_failed")
    if error is not None:
        result["error"] = error
    retry_at = sanitize_public_timestamp(value.get("retry_at"))
    if retry_at is not None:
        result["retry_at"] = retry_at
    if value.get("submission_mode") == "pipeline":
        result["submission_mode"] = "pipeline"
    processing_job_id = normalize_public_identifier(job_id)
    if processing_job_id:
        result["processing_job_id"] = processing_job_id
    if isinstance(value.get("resume_available"), bool):
        result["resume_available"] = bool(value["resume_available"] and result.get("session_id") and result.get("executor_attempt_id"))
    reason = normalize_public_code(value.get("resume_reason"))
    if reason:
        result["resume_reason"] = reason
    snapshot = sanitize_visual_snapshot_summary(value.get("visual_match_snapshot"))
    if snapshot is not None:
        result["visual_match_snapshot"] = snapshot
    return result


def public_processing_warning(value: Mapping[str, object]) -> dict[str, object]:
    """为可恢复的图片处理警告建立固定字段集合。"""
    if not isinstance(value, Mapping):
        return {"error": "auto_rename_warning", "message": "自动重命名未完成"}
    result: dict[str, object] = {}
    stage = value.get("stage")
    if isinstance(stage, str) and stage in PUBLIC_STAGE_NAMES:
        result["stage"] = stage
    error = normalize_public_code(value.get("error"), fallback="auto_rename_warning") or "auto_rename_warning"
    result["error"] = error
    result["message"] = sanitize_public_message(value.get("message"), fallback="自动重命名未完成") or "自动重命名未完成"
    if isinstance(value.get("recoverable"), bool):
        result["recoverable"] = value["recoverable"]
    return result
