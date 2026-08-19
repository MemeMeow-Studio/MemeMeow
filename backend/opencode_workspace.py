"""OpenCode workspace 解析、目录安全校验和 capability 契约。

该模块位于任务控制面与 OpenCodeRunner 之间，只消费已经通过任务 claim
恢复的可信上下文。它不读取普通请求字段，也不把 scope 或物理路径暴露给
executor 请求；宿主适配层通过 provider 提供 opaque selector 对应的目录视图。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol

SELECTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ATTEMPT_ID_RE = TASK_ID_RE
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
CAPABILITY_AUDIENCE = "mememeow-agent-executor"
CAPABILITY_VERSION = 1


class WorkspaceResolutionError(RuntimeError):
    """workspace provider 无法安全解析可信上下文时使用的稳定错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class WorkspaceCapabilityError(WorkspaceResolutionError):
    """workspace capability 缺失、签名无效或绑定不一致时使用的错误。"""


@dataclass(frozen=True)
class TrustedWorkspaceContext:
    """一次已完成 claim 校验的 workspace 解析输入。

    ``scope_id``、``task_id`` 和 ``attempt_id`` 只能由后台 Worker 或宿主
    provider 装配；普通 API payload 不得直接构造并传入该对象。``selector``
    仅用于重试时核对持久 attempt 事实，不能覆盖 provider 的可信映射。
    """

    task_id: str
    attempt_id: str
    scope_id: str
    selector: str | None = None
    session_id: str | None = None
    resume_of_attempt_id: str | None = None
    image_relative_path: str | None = None

    def __post_init__(self) -> None:
        """校验内部任务标识和恢复绑定，避免 provider 接收任意路径值。"""
        if not TASK_ID_RE.fullmatch(self.task_id) or not ATTEMPT_ID_RE.fullmatch(self.attempt_id):
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "任务或 attempt 标识无效")
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "任务 scope 无效")
        if self.selector is not None and not SELECTOR_RE.fullmatch(self.selector):
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "workspace selector 无效")
        if self.session_id is not None and not SESSION_ID_RE.fullmatch(self.session_id):
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "session 标识无效")
        if self.resume_of_attempt_id is not None and not ATTEMPT_ID_RE.fullmatch(self.resume_of_attempt_id):
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "恢复来源 attempt 无效")
        if self.image_relative_path is not None:
            _validate_relative_path(self.image_relative_path, code="opencode_workspace_mismatch")


@dataclass(frozen=True)
class ResolvedWorkspace:
    """一次 OpenCode 执行使用的不可变目录描述。

    ``directory`` 同时是 CLI ``--dir`` 和子进程 ``cwd``；``config_file`` 和
    ``config_dir`` 位于当前 Task 临时目录，以便并发任务拥有各自的精确权限规则；
    ``db_path`` 在同一 runtime 内对所有 selector 固定不变。其余根目录由 provider
    装配，runner 只允许用它们解析当前图片、临时数据和结果文件。
    """

    selector: str
    directory: Path
    config_file: Path
    config_dir: Path
    images_root: Path
    metadata_root: Path
    skill_root: Path
    task_scratch_root: Path
    task_results_root: Path
    db_path: Path
    local: bool = False
    _permission_rules: tuple[tuple[str, str], ...] = field(default_factory=tuple, repr=False)

    @property
    def result_path(self) -> Path:
        """返回当前 Task 的受控最终结果路径。"""
        return self.task_results_root / "result.json.tmp"

    @property
    def draft_path(self) -> Path:
        """返回当前 Task 的受控草稿路径。"""
        return self.task_results_root / "result.json.draft"

    @property
    def permission_rules(self) -> tuple[tuple[str, str], ...]:
        """返回按 OpenCode 最后匹配优先顺序生成的外部目录规则。"""
        return self._permission_rules

    def image_path(self, relative: str) -> Path:
        """将受信任务的相对图片路径解析到当前 workspace 只读视图。"""
        return _safe_child(self.images_root, _validate_relative_path(relative, code="agent_image_path_forbidden"), file_required=True, code="agent_image_path_forbidden")

    def metadata_path(self, relative: str) -> Path:
        """将 metadata 相对路径解析到当前 workspace 只读视图。"""
        return _safe_child(self.metadata_root, _validate_relative_path(relative, code="opencode_workspace_invalid"), file_required=False, code="opencode_workspace_invalid")


class WorkspaceProvider(Protocol):
    """可信任务上下文到受控 OpenCode workspace 的最小 provider 协议。"""

    def resolve(self, context: TrustedWorkspaceContext) -> ResolvedWorkspace:
        """解析一次任务 workspace；失败时不得启动任何 OpenCode 子进程。"""


class MissingWorkspaceProvider:
    """non-local 未装配 provider 时的 fail-closed 占位实现。"""

    def resolve(self, context: TrustedWorkspaceContext) -> ResolvedWorkspace:
        """拒绝所有任务，避免调用方退回 local workspace。"""
        del context
        raise WorkspaceResolutionError("opencode_workspace_provider_missing", "未配置 workspace provider")


class CapabilityProvider(Protocol):
    """可选的 workspace capability 签发协议。"""

    def capability(self, context: TrustedWorkspaceContext, resolved: ResolvedWorkspace) -> str | None:
        """为当前 attempt 返回绑定 selector 的短期 capability。"""


def _validate_relative_path(value: str, *, code: str) -> PurePosixPath:
    """限制图片和 metadata 的 POSIX 相对路径，拒绝绝对路径和父级跳转。"""
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise WorkspaceResolutionError(code, "相对路径无效")
    raw_parts = value.split("/")
    if any(not part or part in {".", ".."} for part in raw_parts) or any(ord(character) < 0x20 for character in value):
        raise WorkspaceResolutionError(code, "相对路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise WorkspaceResolutionError(code, "相对路径无效")
    return path


def _path_parts(path: Path) -> list[Path]:
    """返回从根到目标的完整路径节点，供逐级 lstat 校验。"""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    result: list[Path] = [current]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return result


def validate_directory_path(path: Path, *, create: bool = False, allow_missing_leaf: bool = False, code: str = "opencode_workspace_invalid") -> Path:
    """逐级 lstat 校验目录 containment、节点类型和符号链接。

    ``create`` 只允许创建缺失目录；任何已经存在的符号链接、普通文件或
    其它特殊节点都会稳定失败。该检查在子进程和结果目录副作用之前执行。
    """
    path = Path(os.path.abspath(path))
    nodes = _path_parts(path)
    for index, node in enumerate(nodes):
        try:
            info = node.lstat()
        except FileNotFoundError:
            if not create:
                if allow_missing_leaf and index == len(nodes) - 1:
                    return path
                raise WorkspaceResolutionError(code, "workspace 目录不存在") from None
            try:
                node.mkdir()
            except FileExistsError:
                try:
                    info = node.lstat()
                except OSError as exc:
                    raise WorkspaceResolutionError(code, "workspace 目录无法检查") from exc
            except OSError as exc:
                raise WorkspaceResolutionError(code, "workspace 目录无法创建") from exc
            else:
                continue
        except OSError as exc:
            raise WorkspaceResolutionError(code, "workspace 目录无法检查") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceResolutionError(code, "workspace 路径包含符号链接或非目录节点")
    return path


def validate_file_path(path: Path, *, allow_missing: bool = False, code: str = "opencode_workspace_invalid") -> Path:
    """逐级 lstat 校验普通文件路径，拒绝符号链接和特殊节点。"""
    path = Path(os.path.abspath(path))
    validate_directory_path(path.parent, code=code)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return path
        raise WorkspaceResolutionError(code, "workspace 文件不存在") from None
    except OSError as exc:
        raise WorkspaceResolutionError(code, "workspace 文件无法检查") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceResolutionError(code, "workspace 文件包含符号链接或非普通文件")
    return path


def _safe_child(root: Path, relative: PurePosixPath, *, file_required: bool, code: str) -> Path:
    """在已校验 root 下解析相对路径并重复执行节点类型检查。"""
    validate_directory_path(root, code=code)
    candidate = root.joinpath(*relative.parts)
    root_abs = Path(os.path.abspath(root))
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        candidate_abs.relative_to(root_abs)
    except ValueError as exc:
        raise WorkspaceResolutionError(code, "路径超出 workspace 根") from exc
    parent = candidate_abs.parent
    validate_directory_path(parent, code=code)
    try:
        info = candidate_abs.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceResolutionError(code, "文件不存在") from exc
    except OSError as exc:
        raise WorkspaceResolutionError(code, "文件无法检查") from exc
    if stat.S_ISLNK(info.st_mode) or (file_required and not stat.S_ISREG(info.st_mode)):
        raise WorkspaceResolutionError(code, "文件包含符号链接或节点类型无效")
    if not file_required and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise WorkspaceResolutionError(code, "文件节点类型无效")
    return candidate_abs


def _containment(path: Path, root: Path, *, code: str) -> None:
    """确认 path 位于 root 内且两者所有祖先都不是符号链接。"""
    path_abs = Path(os.path.abspath(path))
    root_abs = Path(os.path.abspath(root))
    try:
        path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise WorkspaceResolutionError(code, "workspace 路径逃出配置根") from exc
    validate_directory_path(root_abs, code=code)
    validate_directory_path(path_abs, create=True, code=code)


def build_external_directory_rules(workspace: ResolvedWorkspace) -> tuple[tuple[str, str], ...]:
    """生成 catch-all deny 后的精确只读输入和受控写入规则。"""
    def subtree(path: Path) -> str:
        """返回 OpenCode 外部目录 glob，保持绝对路径来自 provider。"""
        return f"{Path(os.path.abspath(path)).as_posix()}/**"

    result_directory = Path(os.path.abspath(workspace.task_results_root)).as_posix()
    return (
        ("*", "deny"),
        (subtree(workspace.skill_root), "allow"),
        (subtree(workspace.images_root), "allow"),
        (subtree(workspace.metadata_root), "allow"),
        (subtree(workspace.task_scratch_root), "allow"),
        # OpenCode 对外部文件先请求其父目录 ``<dir>/*``；仅写两个精确文件
        # 规则无法允许首次创建，因此这里只放行当前 Task 结果目录的父级检查，
        # 实际可写文件由 ``edit`` 规则继续收窄。
        (f"{result_directory}/*", "allow"),
        (Path(os.path.abspath(workspace.draft_path)).as_posix(), "allow"),
        (Path(os.path.abspath(workspace.result_path)).as_posix(), "allow"),
    )


def build_edit_permission_rules(
    *,
    task_scratch_root: Path,
    config_file: Path,
    config_dir: Path,
    draft_path: Path,
    result_path: Path,
) -> tuple[tuple[str, str], ...]:
    """限制 OpenCode ``edit``、``write`` 和 ``apply_patch`` 的可写路径。

    ``external_directory`` 只有 allow/deny 目录边界，不能表达只读输入；
    OpenCode 将三个编辑工具统一映射为 ``edit`` 权限，因此这里默认拒绝，
    仅开放当前 Task 临时目录和两个结果文件，并再次保护服务端生成的配置。
    规则同时生成绝对、根相对和 workspace 相对形式，兼容 OpenCode 在无 Git
    workspace 下把编辑路径相对化到 ``/`` 的行为。
    """
    def pattern(path: Path) -> str:
        """把 provider 路径转换为不依赖 worktree 根的权限模式。"""
        absolute = Path(os.path.abspath(path)).as_posix().lstrip("/")
        return f"**/{absolute}"

    workspace_directory = Path(os.path.abspath(task_scratch_root)).parent.parent

    def patterns(path: Path) -> tuple[str, ...]:
        """返回 OpenCode 可能使用的绝对、根相对和 workspace 相对模式。"""
        absolute = Path(os.path.abspath(path)).as_posix()
        values = [absolute, absolute.lstrip("/"), pattern(path)]
        try:
            values.append(Path(path).resolve().relative_to(workspace_directory).as_posix())
        except (OSError, ValueError):
            pass
        return tuple(dict.fromkeys(values))

    def subtree(path: Path) -> tuple[str, ...]:
        """返回当前目录及其后代的全部编辑权限模式。"""
        return tuple(f"{value}/**" for value in patterns(path))

    def exact(path: Path) -> tuple[str, ...]:
        """返回一个文件的全部编辑权限模式。"""
        return patterns(path)

    rules: list[tuple[str, str]] = [("*", "deny")]
    rules.extend((value, "allow") for value in subtree(task_scratch_root))
    rules.extend((value, "allow") for value in exact(draft_path))
    rules.extend((value, "allow") for value in exact(result_path))
    # 配置虽然位于 Task 临时目录，但必须保持由服务端原子生成，不能被
    # Agent 通过普通文件工具改写后影响后续 OpenCode 调用。
    rules.extend((value, "deny") for value in exact(config_file))
    rules.extend((value, "deny") for value in subtree(config_dir))
    return tuple(rules)


def capability_claims(*, task_id: str, attempt_id: str, selector: str, exp: int, session_id: str | None = None, resume_of_attempt_id: str | None = None, audience: str = CAPABILITY_AUDIENCE) -> dict[str, object]:
    """构造 workspace capability 的固定 claims 集合。"""
    if not TASK_ID_RE.fullmatch(task_id) or not ATTEMPT_ID_RE.fullmatch(attempt_id) or not SELECTOR_RE.fullmatch(selector):
        raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability 绑定标识无效")
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= 0:
        raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability 期限无效")
    if audience != CAPABILITY_AUDIENCE:
        raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability 受众无效")
    if (session_id is None) != (resume_of_attempt_id is None):
        raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability 恢复绑定不完整")
    claims: dict[str, object] = {
        "v": CAPABILITY_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workspace_selector": selector,
        "audience": audience,
        "exp": exp,
    }
    if session_id is not None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability session 无效")
        claims["session_id"] = session_id
    if resume_of_attempt_id is not None:
        if not ATTEMPT_ID_RE.fullmatch(resume_of_attempt_id):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability 恢复来源无效")
        claims["resume_of_attempt_id"] = resume_of_attempt_id
    return claims


def _b64(value: bytes) -> str:
    """将 capability 片段编码为无填充 URL-safe Base64。"""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    """解码无填充 URL-safe Base64，格式错误统一失败。"""
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("base64_invalid")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class WorkspaceCapabilitySigner:
    """用独立服务密钥签发和验证短期 workspace capability。"""

    def __init__(self, secret: str | bytes | None, *, ttl_seconds: int = 300, clock: Callable[[], float] | None = None) -> None:
        """保存 capability 专用密钥；不会复用 provider/API 长期模型 key。"""
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        else:
            secret_bytes = secret
        self.secret = secret_bytes or b""
        self.ttl_seconds = max(1, min(int(ttl_seconds), 3600))
        self.clock = clock or time.time

    @property
    def configured(self) -> bool:
        """返回是否配置了非空签名材料。"""
        return bool(self.secret)

    def sign(self, claims: Mapping[str, object]) -> str:
        """签发 canonical JSON capability，调用者必须提供已绑定 claims。"""
        if not self.configured:
            raise WorkspaceCapabilityError("opencode_workspace_capability_unavailable", "未配置 workspace capability 验证材料")
        value = dict(claims)
        try:
            validated = capability_claims(
                task_id=value["task_id"],
                attempt_id=value["attempt_id"],
                selector=value["workspace_selector"],
                exp=value["exp"],
                session_id=value.get("session_id"),
                resume_of_attempt_id=value.get("resume_of_attempt_id"),
                audience=value["audience"],
            )
        except (KeyError, TypeError, WorkspaceCapabilityError):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability claims 不完整或无效") from None
        # capability 是固定协议；拒绝额外 claims，避免签发方和验证方对未定义字段产生
        # 不一致解释，也避免把任意业务数据带入可转发的 token。
        if set(value) != set(validated):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "capability claims 包含未知字段")
        value = validated
        raw = _b64(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = _b64(hmac.new(self.secret, raw.encode("ascii"), hashlib.sha256).digest())
        return f"v1.{raw}.{signature}"

    def issue(self, *, task_id: str, attempt_id: str, selector: str, session_id: str | None = None, resume_of_attempt_id: str | None = None) -> str:
        """按当前时间签发带业务、attempt、selector 和恢复绑定的 capability。"""
        claims = capability_claims(
            task_id=task_id,
            attempt_id=attempt_id,
            selector=selector,
            exp=int(self.clock()) + self.ttl_seconds,
            session_id=session_id,
            resume_of_attempt_id=resume_of_attempt_id,
        )
        return self.sign(claims)

    def verify(self, token: str, *, task_id: str, attempt_id: str, selector: str, session_id: str | None = None, resume_of_attempt_id: str | None = None, now: int | None = None) -> dict[str, object]:
        """验证签名、受众、期限以及当前 Task/attempt/session/selector 绑定。"""
        if not self.configured:
            raise WorkspaceCapabilityError("opencode_workspace_capability_unavailable", "未配置 workspace capability 验证材料")
        if (session_id is None) != (resume_of_attempt_id is None):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "当前恢复绑定不完整")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability 无效")
        try:
            version, encoded, supplied = token.split(".")
            if version != "v1":
                raise ValueError("version_invalid")
            expected = _b64(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature_invalid")
            claims = json.loads(_unb64(encoded).decode("utf-8"))
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability 无效") from None
        if not isinstance(claims, dict):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability claims 无效")
        required = {"v", "task_id", "attempt_id", "workspace_selector", "audience", "exp"}
        if not required.issubset(claims) or claims.get("v") != CAPABILITY_VERSION or claims.get("audience") != CAPABILITY_AUDIENCE:
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability claims 不完整")
        allowed_claims = required | {"session_id", "resume_of_attempt_id"}
        if set(claims) - allowed_claims:
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability 包含未知字段")
        if (
            not isinstance(claims.get("task_id"), str)
            or not TASK_ID_RE.fullmatch(str(claims["task_id"]))
            or not isinstance(claims.get("attempt_id"), str)
            or not ATTEMPT_ID_RE.fullmatch(str(claims["attempt_id"]))
            or not isinstance(claims.get("workspace_selector"), str)
            or not SELECTOR_RE.fullmatch(str(claims["workspace_selector"]))
        ):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability 标识无效")
        expiry = claims.get("exp")
        current = int(self.clock()) if now is None else int(now)
        if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry <= current:
            raise WorkspaceCapabilityError("opencode_workspace_capability_expired", "workspace capability 已过期")
        expected_values = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workspace_selector": selector,
        }
        if session_id is not None:
            expected_values["session_id"] = session_id
        if resume_of_attempt_id is not None:
            expected_values["resume_of_attempt_id"] = resume_of_attempt_id
        for key, expected_value in expected_values.items():
            if claims.get(key) != expected_value:
                raise WorkspaceCapabilityError("opencode_workspace_mismatch", "workspace capability 绑定不一致")
        if session_id is None and "session_id" in claims:
            raise WorkspaceCapabilityError("opencode_workspace_mismatch", "workspace capability 包含非当前 session")
        if resume_of_attempt_id is None and "resume_of_attempt_id" in claims:
            raise WorkspaceCapabilityError("opencode_workspace_mismatch", "workspace capability 包含非当前恢复来源")
        return {str(key): value for key, value in claims.items()}


class _BaseProvider:
    """共享 provider 的目录、selector 和 capability 辅助逻辑。"""

    def __init__(self, *, runtime_root: Path, signer: WorkspaceCapabilitySigner | None = None, capability_ttl_seconds: int = 300) -> None:
        self.runtime_root = Path(runtime_root).expanduser()
        self.signer = signer
        self.capability_ttl_seconds = capability_ttl_seconds

    def capability(self, context: TrustedWorkspaceContext, resolved: ResolvedWorkspace) -> str | None:
        """按已解析 selector 签发当前 attempt 的 workspace capability。"""
        if self.signer is None or not self.signer.configured:
            return None
        return self.signer.issue(
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            selector=resolved.selector,
            session_id=context.session_id,
            resume_of_attempt_id=context.resume_of_attempt_id,
        )

    def _resolved(
        self,
        *,
        selector: str,
        directory: Path,
        images_root: Path,
        metadata_root: Path,
        skill_root: Path,
        task_id: str,
        local: bool,
        create_workspace: bool = True,
        allow_skill_symlink: bool = False,
    ) -> ResolvedWorkspace:
        """构造并验证一次 workspace 描述，所有根路径必须来自 provider。"""
        if not SELECTOR_RE.fullmatch(selector):
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace selector 无效")
        runtime_root = validate_directory_path(self.runtime_root, create=True)
        directory = Path(directory)
        if create_workspace:
            validate_directory_path(directory, create=True)
        else:
            validate_directory_path(directory)
        # 配置按 Task 独立生成；共享 workspace 内同时运行多个任务时，精确结果
        # allow 规则不会被另一个任务最后一次写入的配置覆盖。
        task_scratch = directory / "tasks" / task_id
        config_file = task_scratch / "opencode.json"
        config_dir = task_scratch / ".opencode"
        validate_directory_path(images_root, create=local)
        validate_directory_path(metadata_root, create=local)
        if allow_skill_symlink and skill_root.is_symlink():
            # local 历史 runtime 用相对链接挂载既有 skill；该兼容例外只由
            # 显式 LocalWorkspaceProvider 使用，外部 selector 仍拒绝所有链接。
            target = skill_root.resolve(strict=True)
            validate_directory_path(target)
        else:
            validate_directory_path(skill_root, create=local)
        task_results = runtime_root / "task-results" / task_id
        db_path = runtime_root / "opencode.db"
        validate_file_path(db_path, allow_missing=True)
        result = ResolvedWorkspace(
            selector=selector,
            directory=directory,
            config_file=config_file,
            config_dir=config_dir,
            images_root=images_root,
            metadata_root=metadata_root,
            skill_root=skill_root,
            task_scratch_root=task_scratch,
            task_results_root=task_results,
            db_path=db_path,
            local=local,
        )
        return replace(result, _permission_rules=build_external_directory_rules(result))


class LocalWorkspaceProvider(_BaseProvider):
    """显式 local 单用户 provider，原地复用既有 runtime/workspace 历史。"""

    def __init__(self, runtime_root: Path, *, image_root: Path, skill_root: Path | None = None, signer: WorkspaceCapabilitySigner | None = None) -> None:
        """保存 local runtime、图片根和可选只读 Skill 根。"""
        super().__init__(runtime_root=runtime_root, signer=signer)
        self.image_root = Path(image_root).expanduser()
        self.skill_root = Path(skill_root).expanduser() if skill_root is not None else None

    def resolve(self, context: TrustedWorkspaceContext) -> ResolvedWorkspace:
        """将 local scope 映射到旧 ``<runtime>/workspace``，拒绝其它 scope。"""
        if context.scope_id != "local":
            raise WorkspaceResolutionError("opencode_workspace_provider_missing", "local provider 不能解析非 local scope")
        workspace = self.runtime_root / "workspace"
        skill = self.skill_root if self.skill_root is not None and self.skill_root.exists() else workspace / ".opencode" / "skills" / "research-meme-context"
        return self._resolved(
            selector="local",
            directory=workspace,
            images_root=self.image_root,
            metadata_root=self.image_root,
            skill_root=skill,
            task_id=context.task_id,
            local=True,
            allow_skill_symlink=True,
        )


class DirectoryWorkspaceProvider(_BaseProvider):
    """按受信 scope 映射固定目录布局的通用外部 provider。

    目录布局为 ``<workspace_root>/<selector>/{workspace,images,metadata,skills}``；
    适配宿主负责准备 scope 视图和生命周期，provider 只允许创建当前 workspace
    的配置、Task 临时目录和结果目录。
    """

    def __init__(self, runtime_root: Path, workspace_root: Path, *, selector_for_scope: Callable[[str], str] | None = None, signer: WorkspaceCapabilitySigner | None = None, skill_root: Path | None = None) -> None:
        """保存受控配置根和可信 scope-to-selector 映射回调。"""
        super().__init__(runtime_root=runtime_root, signer=signer)
        self.workspace_root = Path(workspace_root).expanduser()
        self.selector_for_scope = selector_for_scope or self._default_selector
        self.skill_root = Path(skill_root).expanduser() if skill_root is not None else None

    @staticmethod
    def _default_selector(scope_id: str) -> str:
        """将可信 scope 转为不可逆、稳定且不含路径语义的 selector。"""
        return "scope-" + hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:32]

    def resolve(self, context: TrustedWorkspaceContext) -> ResolvedWorkspace:
        """解析外部 selector，拒绝未知目录、路径穿越和符号链接节点。"""
        try:
            mapped = self.selector_for_scope(context.scope_id)
        except Exception as exc:  # noqa: BLE001 - provider 边界统一失败关闭
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace selector 无法解析") from exc
        if not isinstance(mapped, str) or not SELECTOR_RE.fullmatch(mapped):
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace selector 无效")
        if mapped == "local":
            # external provider 不能借用旧 local 保留字；executor 会把该值视为
            # 无 capability 的兼容路径，混用会绕过非 local 的签名边界。
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace selector 保留字无效")
        if context.selector is not None and context.selector != mapped:
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "workspace selector 与持久任务事实不一致")
        # 外部 workspace 根和 selector 目录必须由宿主预装配；解析请求不能借机
        # 创建新的 scope 目录或把未知 selector 变成可执行路径。
        root = validate_directory_path(self.workspace_root, create=False)
        base = root / mapped
        # selector 目录必须由宿主预装配；未知 selector 不得因为 runner 的 mkdir
        # 变成一个新的可执行 workspace。
        validate_directory_path(base, code="opencode_workspace_invalid")
        skill = self.skill_root or base / "skills"
        return self._resolved(
            selector=mapped,
            directory=base / "workspace",
            images_root=base / "images",
            metadata_root=base / "metadata",
            skill_root=skill,
            task_id=context.task_id,
            local=False,
            create_workspace=False,
        )


def capability_for_provider(provider: object, context: TrustedWorkspaceContext, resolved: ResolvedWorkspace) -> str | None:
    """调用 provider 的 capability 签发扩展点，缺失时保持 fail-closed 语义。"""
    issue = getattr(provider, "capability", None)
    if callable(issue):
        try:
            value = issue(context, resolved)
        except WorkspaceResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - capability 边界统一收敛
            raise WorkspaceCapabilityError("opencode_workspace_capability_unavailable", "workspace capability 无法签发") from exc
        if value is not None and (not isinstance(value, str) or not value):
            raise WorkspaceCapabilityError("opencode_workspace_capability_invalid", "workspace capability 格式无效")
        return value
    return None


__all__ = [
    "ATTEMPT_ID_RE",
    "CAPABILITY_AUDIENCE",
    "DirectoryWorkspaceProvider",
    "LocalWorkspaceProvider",
    "MissingWorkspaceProvider",
    "ResolvedWorkspace",
    "SELECTOR_RE",
    "TrustedWorkspaceContext",
    "WorkspaceCapabilityError",
    "WorkspaceCapabilitySigner",
    "WorkspaceProvider",
    "WorkspaceResolutionError",
    "build_edit_permission_rules",
    "build_external_directory_rules",
    "capability_claims",
    "capability_for_provider",
    "validate_directory_path",
    "validate_file_path",
]
