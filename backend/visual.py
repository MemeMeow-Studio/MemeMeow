"""DINOv2 视觉推理、视觉向量校验和 scope 内匹配服务。

主后端只通过 ``VisualInferenceClient`` 调用独立 CPU 推理服务；匹配查询只读取
PostgreSQL 已持久化向量，绝不会在 Agent 请求期间加载模型或触发即时推理。
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import math
import os
import pickle
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from PIL import Image, ImageFile, UnidentifiedImageError

from backend.database import (
    VISUAL_EMBEDDING_DIMENSIONS,
    DatabaseError,
    DatabaseResources,
    ScopeContext,
    utcnow,
    validate_visual_vector,
)
from backend.visual_models import (
    ACTIVE_VISUAL_MODEL_ID,
    VISUAL_MODEL_SPECS,
    active_visual_model_spec,
    source_repository_valid,
    visual_model_spec,
)
from backend.visual_snapshot import build_visual_match_snapshot


_ACTIVE_VISUAL_SPEC = active_visual_model_spec()
VISUAL_MODEL_ID = ACTIVE_VISUAL_MODEL_ID
VISUAL_DIMENSIONS = VISUAL_EMBEDDING_DIMENSIONS
VISUAL_PREPROCESS_VERSION = _ACTIVE_VISUAL_SPEC.preprocess_version
VISUAL_IMAGE_SIZE = 224
VISUAL_CHECKPOINT_FILENAME = _ACTIVE_VISUAL_SPEC.checkpoint_filename
# 保留历史常量，便于旧部署脚本读取 DINOv3 清单但不会将其作为活动模型加载。
DINOV2_CHECKPOINT_FILENAME = VISUAL_MODEL_SPECS["dinov2_vitb14"].checkpoint_filename
DINOV3_CHECKPOINT_FILENAME = VISUAL_MODEL_SPECS["dinov3_vith16plus"].checkpoint_filename
ImageFile.LOAD_TRUNCATED_IMAGES = False


class VisualEmbeddingError(RuntimeError):
    """视觉推理或输入校验失败，携带稳定错误码和 HTTP 状态。"""

    def __init__(self, code: str, message: str | None = None, *, status_code: int = 503, retryable: bool = True):
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class VisualSearchError(RuntimeError):
    """scope-bound 视觉匹配业务错误。"""

    def __init__(self, code: str, message: str | None = None, *, status_code: int = 409):
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code


def validate_embedding(vector: Sequence[float], *, dimensions: int = VISUAL_DIMENSIONS) -> list[float]:
    """对外提供统一视觉向量校验入口，失败时转换为视觉稳定错误。"""
    try:
        return validate_visual_vector(vector, dimensions=dimensions)
    except DatabaseError as exc:
        code = {
            "visual_embedding_dimensions_mismatch": "visual_embedding_dimensions_mismatch",
            "visual_embedding_non_finite": "visual_embedding_non_finite",
            "visual_embedding_zero_norm": "visual_embedding_zero_norm",
        }.get(exc.code, "visual_embedding_invalid")
        raise VisualEmbeddingError(code, status_code=502, retryable=False) from exc


def _sha256_file(path: Path) -> str:
    """计算权重文件 SHA-256，供服务启动时审核只读权重。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class VisualModelIdentity:
    """写入向量和任务 payload 的不可变模型身份。"""

    model: str = VISUAL_MODEL_ID
    dimensions: int = VISUAL_DIMENSIONS
    preprocess_version: str = VISUAL_PREPROCESS_VERSION

    def as_dict(self) -> dict[str, object]:
        """返回可序列化的模型身份。"""
        return {"model": self.model, "dimensions": self.dimensions, "preprocess_version": self.preprocess_version}


def identity_from_settings(settings: Any) -> VisualModelIdentity:
    """从服务端配置读取视觉身份，忽略客户端和 Agent 传入的覆盖值。"""
    return VisualModelIdentity(
        model=str(getattr(settings, "visual_model", VISUAL_MODEL_ID)),
        dimensions=int(getattr(settings, "visual_model_dimensions", VISUAL_DIMENSIONS)),
        preprocess_version=str(getattr(settings, "visual_preprocess_version", VISUAL_PREPROCESS_VERSION)),
    )


class VisualModelRunner:
    """独立视觉服务内的单实例 CPU 模型适配器。

    默认不下载权重。部署必须通过只读路径提供官方 ``.pth`` 权重和固定版本的
    当前模型的官方源码；测试可注入 ``model_factory`` 或 ``model``，从而在没有官方权重的
    环境中验证完整错误和协议。
    """

    def __init__(self, settings: Any, *, model: Any | None = None, model_factory: Callable[[], Any] | None = None):
        self.settings = settings
        self.identity = identity_from_settings(settings)
        self._model = model
        self._model_factory = model_factory
        self._load_error: VisualEmbeddingError | None = None

    @property
    def configured(self) -> bool:
        """判断服务端是否提供了可读源码、权重和正确 SHA（若要求）。"""
        if self._model is not None or self._model_factory is not None:
            # 测试注入的模型不依赖部署权重文件；生产路径仍必须通过权重配置校验。
            return True
        if not self.identity.model.strip() or self.identity.dimensions <= 0 or not self.identity.preprocess_version.strip():
            return False
        path = getattr(self.settings, "visual_weights_path", None)
        if path is None:
            return False
        try:
            path = Path(path).expanduser()
            if not path.is_file() or not os.access(path, os.R_OK):
                return False
            expected = getattr(self.settings, "visual_weights_sha256", None)
            if expected and _sha256_file(path).lower() != str(expected).lower():
                return False
            source = getattr(self.settings, "visual_model_repo", None)
            spec = visual_model_spec(self.identity.model)
            if spec is not None and (
                self.identity.dimensions != spec.dimensions
                or self.identity.preprocess_version != spec.preprocess_version
                or not spec.runtime_supported
            ):
                return False
            return self._source_repository_valid(source, self.identity.model)
        except OSError:
            return False

    @staticmethod
    def _source_repository_valid(source: Any, model: str = VISUAL_MODEL_ID) -> bool:
        """检查模型清单要求的官方源码，不执行源码或联网访问。"""
        return source_repository_valid(source, model)

    def _configuration_error(self) -> VisualEmbeddingError:
        """构造不泄露源码或权重绝对路径的配置错误。"""
        if not self.identity.model.strip() or self.identity.dimensions <= 0 or not self.identity.preprocess_version.strip():
            return VisualEmbeddingError("visual_model_not_configured", "视觉模型配置不完整", status_code=503, retryable=False)
        spec = visual_model_spec(self.identity.model)
        if spec is not None:
            if not spec.runtime_supported:
                return VisualEmbeddingError("visual_model_migration_required", "当前视觉模型需要新的数据库迁移", status_code=503, retryable=False)
            if self.identity.dimensions != spec.dimensions or self.identity.preprocess_version != spec.preprocess_version:
                return VisualEmbeddingError("visual_model_identity_invalid", "视觉模型身份与发布清单不一致", status_code=503, retryable=False)
        path = getattr(self.settings, "visual_weights_path", None)
        if not path:
            return VisualEmbeddingError("visual_model_not_configured", "视觉模型权重尚未配置", status_code=503, retryable=False)
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_file() or not os.access(candidate, os.R_OK):
                return VisualEmbeddingError("visual_weights_unreadable", "视觉模型权重不可读", status_code=503, retryable=True)
            expected = getattr(self.settings, "visual_weights_sha256", None)
            if expected and _sha256_file(candidate).lower() != str(expected).lower():
                return VisualEmbeddingError("visual_weights_checksum_mismatch", "视觉模型权重校验失败", status_code=503, retryable=False)
        except OSError:
            return VisualEmbeddingError("visual_weights_unreadable", "视觉模型权重不可读", status_code=503, retryable=True)
        source = getattr(self.settings, "visual_model_repo", None)
        if source is None:
            return VisualEmbeddingError("visual_model_source_not_configured", "视觉模型源码尚未配置", status_code=503, retryable=False)
        if not self._source_repository_valid(source, self.identity.model):
            return VisualEmbeddingError("visual_model_source_unreadable", "视觉模型源码不可读", status_code=503, retryable=False)
        return VisualEmbeddingError("visual_model_runtime_unavailable", "视觉模型运行时不可用", status_code=503, retryable=True)

    @staticmethod
    def _load_checkpoint(torch: Any, path: Path) -> Mapping[str, Any]:
        """读取官方 ``.pth`` state dict，禁止执行 checkpoint 中的任意 Python 对象。"""
        try:
            checkpoint = torch.load(str(path), map_location="cpu", weights_only=True)
        except (OSError, RuntimeError, ValueError, TypeError, EOFError, pickle.UnpicklingError, IndexError, KeyError) as exc:
            raise VisualEmbeddingError(
                "visual_checkpoint_format_invalid",
                "视觉模型 checkpoint 格式无效",
                status_code=503,
                retryable=False,
            ) from exc
        if not isinstance(checkpoint, Mapping):
            raise VisualEmbeddingError(
                "visual_checkpoint_format_invalid",
                "视觉模型 checkpoint 格式无效",
                status_code=503,
                retryable=False,
            )
        return checkpoint

    def _load_official_model(self, torch: Any, path: Path) -> Any:
        """按当前模型清单构造官方 backbone 并严格加载 state dict。"""
        spec = visual_model_spec(self.identity.model)
        if spec is None or not spec.runtime_supported:
            raise VisualEmbeddingError(
                "visual_model_migration_required",
                "当前视觉模型需要新的数据库迁移",
                status_code=503,
                retryable=False,
            )
        source = Path(getattr(self.settings, "visual_model_repo")).expanduser()
        source_string = str(source)
        path_added = source_string not in sys.path
        if path_added:
            sys.path.insert(0, source_string)
        try:
            # 先验证 checkpoint 顶层类型，再分配模型参数内存，避免损坏文件触发无意义构造。
            checkpoint = self._load_checkpoint(torch, path)
            try:
                package = importlib.import_module(spec.source_package)
                backbones = importlib.import_module(f"{spec.source_package}.hub.backbones")
                builder = getattr(backbones, spec.backbone_name)
            except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
                raise VisualEmbeddingError(
                    "visual_model_source_unreadable",
                    "视觉模型源码无法导入",
                    status_code=503,
                    retryable=False,
                ) from exc
            module_file = getattr(package, "__file__", None)
            if module_file is not None and Path(module_file).expanduser().resolve() != (source / spec.source_package / "__init__.py").resolve():
                raise VisualEmbeddingError(
                    "visual_model_source_unreadable",
                    "视觉模型源码版本不匹配",
                    status_code=503,
                    retryable=False,
                )
            try:
                model = builder(pretrained=False)
            except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
                raise VisualEmbeddingError(
                    "visual_model_architecture_mismatch",
                    "视觉模型源码架构不可用",
                    status_code=503,
                    retryable=False,
                ) from exc
            try:
                model.load_state_dict(checkpoint, strict=True)
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                raise VisualEmbeddingError(
                    "visual_model_architecture_mismatch",
                    "视觉模型 checkpoint 与架构不匹配",
                    status_code=503,
                    retryable=False,
                ) from exc
            try:
                model = model.float()
            except (AttributeError, RuntimeError, TypeError) as exc:
                raise VisualEmbeddingError(
                    "visual_model_runtime_unavailable",
                    "视觉模型无法切换到 FP32",
                    status_code=503,
                    retryable=True,
                ) from exc
            embed_dim = getattr(model, "embed_dim", getattr(model, "num_features", None))
            if embed_dim is not None and int(embed_dim) != self.identity.dimensions:
                raise VisualEmbeddingError(
                    "visual_model_architecture_mismatch",
                    "视觉模型输出维度与配置不一致",
                    status_code=503,
                    retryable=False,
                )
            return model
        finally:
            if path_added:
                try:
                    sys.path.remove(source_string)
                except ValueError:
                    pass

    def load(self) -> Any:
        """加载一次固定模型，直接读取官方 checkpoint，禁止隐式下载或伪造成功。"""
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            raise self._load_error
        if not self.configured:
            self._load_error = self._configuration_error()
            raise self._load_error
        try:
            import torch

            torch.set_num_threads(int(getattr(self.settings, "visual_cpu_threads", 4)))
            try:
                torch.set_num_interop_threads(int(getattr(self.settings, "visual_cpu_interop_threads", 1)))
            except RuntimeError:
                # 解释器中已有其他 torch 调用时线程数不可再修改，保持当前全局设置。
                pass
            if self._model_factory is not None:
                self._model = self._model_factory()
            else:
                path = Path(getattr(self.settings, "visual_weights_path")).expanduser()
                self._model = self._load_official_model(torch, path)
            self._model.eval()
            return self._model
        except VisualEmbeddingError as exc:
            self._load_error = exc
            raise
        except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError, IndexError, KeyError) as exc:
            self._load_error = VisualEmbeddingError("visual_model_runtime_unavailable", "视觉模型无法加载", status_code=503, retryable=True)
            raise self._load_error from exc

    @staticmethod
    def _preprocess(content: bytes, *, max_pixels: int = 25_000_000) -> Image.Image:
        """把静态图转 RGB，GIF 只选第一帧，并拒绝超大或损坏输入。"""
        try:
            with Image.open(io.BytesIO(content)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > int(max_pixels):
                    raise VisualEmbeddingError("image_too_large", "图片尺寸超过视觉输入限制", status_code=413, retryable=False)
                if getattr(source, "is_animated", False) or source.format == "GIF":
                    source.seek(0)
                image = source.convert("RGB")
                image.load()
                return image.copy()
        except VisualEmbeddingError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, EOFError) as exc:
            raise VisualEmbeddingError("visual_image_decode_failed", "图片无法解码", status_code=400, retryable=False) from exc

    def _tensor(self, image: Image.Image) -> Any:
        """执行固定 224px RGB 预处理，避免 class/patch/register token 混用。"""
        try:
            import torch
            from torchvision.transforms import Compose, Normalize, Resize, CenterCrop, ToTensor
        except (ImportError, RuntimeError) as exc:
            raise VisualEmbeddingError("visual_model_runtime_unavailable", "视觉预处理运行时不可用", status_code=503, retryable=True) from exc
        transform = Compose(
            [
                Resize(256, antialias=True),
                CenterCrop(VISUAL_IMAGE_SIZE),
                ToTensor(),
                Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
            ]
        )
        return transform(image).unsqueeze(0)

    @staticmethod
    def _extract_class_token(output: Any) -> Any:
        """只接受官方 class-token 表示，不把 patch/register token 当 embedding。"""
        if isinstance(output, Mapping):
            for key in ("x_norm_clstoken", "cls_token", "class_token"):
                if key in output:
                    return output[key]
            if "last_hidden_state" in output:
                output = output["last_hidden_state"]
        if isinstance(output, (tuple, list)):
            output = output[0]
        if getattr(output, "ndim", 0) == 3:
            return output[:, 0, :]
        return output

    def embed(self, content: bytes) -> dict[str, object]:
        """生成单图 FP32 embedding，并返回模型身份和归一化向量。"""
        image = self._preprocess(content, max_pixels=int(getattr(self.settings, "visual_max_pixels", 25_000_000)))
        model = self.load()
        try:
            import torch

            tensor = self._tensor(image).to(dtype=torch.float32)
            with torch.inference_mode():
                output = self._extract_class_token(model(tensor))
            if hasattr(output, "detach"):
                values = output.detach().float().cpu().reshape(-1).tolist()
            else:
                values = list(output)
            vector = validate_embedding(values, dimensions=self.identity.dimensions)
        except VisualEmbeddingError:
            raise
        except (RuntimeError, TypeError, ValueError, IndexError) as exc:
            code = "visual_embedding_dimensions_mismatch" if "dimension" in str(exc).lower() or "shape" in str(exc).lower() else "visual_inference_failed"
            raise VisualEmbeddingError(code, "视觉模型输出无效", status_code=502, retryable=False) from exc
        return {**self.identity.as_dict(), "embedding": vector, "dtype": "float32"}

    def health(self) -> dict[str, object]:
        """返回脱敏健康状态，绝不返回权重路径或 SHA。"""
        if not self.configured:
            error = self._configuration_error()
            return {"status": "degraded", "available": False, "model": self.identity.model, "dimensions": self.identity.dimensions, "preprocess_version": self.identity.preprocess_version, "error": error.code}
        try:
            self.load()
        except VisualEmbeddingError as exc:
            return {"status": "degraded", "available": False, "model": self.identity.model, "dimensions": self.identity.dimensions, "preprocess_version": self.identity.preprocess_version, "error": exc.code}
        return {"status": "ok", "available": True, "model": self.identity.model, "dimensions": self.identity.dimensions, "preprocess_version": self.identity.preprocess_version}


class VisualInferenceClient:
    """主后端到内部视觉服务的最小 HTTP 客户端。"""

    def __init__(self, settings: Any, *, opener: Callable[..., Any] | None = None):
        self.settings = settings
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def _multipart(content: bytes, filename: str) -> tuple[bytes, str]:
        """构造只包含图片字段的 multipart 请求体。"""
        boundary = f"----mememeow-{uuid.uuid4().hex}"
        body = b"--" + boundary.encode() + b"\r\n"
        body += f'Content-Disposition: form-data; name="image"; filename="{Path(filename).name or "image"}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        body += b"--" + boundary.encode() + b"--\r\n"
        return body, f"multipart/form-data; boundary={boundary}"

    def embed(self, content: bytes, *, filename: str = "image") -> dict[str, object]:
        """调用内部 embedding 接口并验证响应模型身份和向量。"""
        if not content:
            raise VisualEmbeddingError("visual_image_decode_failed", "图片内容为空", status_code=400, retryable=False)
        limit = int(getattr(self.settings, "max_upload_size", 20 * 1024 * 1024))
        if len(content) > limit:
            raise VisualEmbeddingError("image_too_large", "图片超过视觉输入大小限制", status_code=413, retryable=False)
        url = str(getattr(self.settings, "visual_internal_url", ""))
        if not url:
            raise VisualEmbeddingError("visual_model_not_configured", "视觉推理服务未配置", status_code=503, retryable=False)
        if getattr(self.settings, "visual_weights_path", None) is None and url.startswith("http://127.0.0.1:8276"):
            # 默认本地地址没有独立服务和权重配置时，返回配置错误而非伪造网络故障。
            raise VisualEmbeddingError("visual_model_not_configured", "视觉模型权重尚未配置", status_code=503, retryable=False)
        body, content_type = self._multipart(content, filename)
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        token = getattr(self.settings, "visual_internal_token", None)
        if token:
            headers["X-MemeMeow-Internal-Token"] = str(token)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=int(getattr(self.settings, "visual_request_timeout_seconds", 120))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError):
                payload = {}
            code = payload.get("error") if isinstance(payload, dict) else None
            raise VisualEmbeddingError(str(code or "visual_service_http_error"), "视觉推理服务请求失败", status_code=exc.code, retryable=exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise VisualEmbeddingError("visual_service_unavailable", "视觉推理服务暂时不可用", status_code=503, retryable=True) from exc
        if not isinstance(payload, dict):
            raise VisualEmbeddingError("visual_service_invalid_response", "视觉推理服务返回格式无效", status_code=502, retryable=False)
        if payload.get("model") != str(getattr(self.settings, "visual_model", VISUAL_MODEL_ID)) or int(payload.get("dimensions", -1)) != int(getattr(self.settings, "visual_model_dimensions", VISUAL_DIMENSIONS)) or payload.get("preprocess_version") != str(getattr(self.settings, "visual_preprocess_version", VISUAL_PREPROCESS_VERSION)):
            raise VisualEmbeddingError("visual_model_identity_mismatch", "视觉推理服务模型身份不一致", status_code=502, retryable=False)
        vector = validate_embedding(payload.get("embedding") or [], dimensions=int(getattr(self.settings, "visual_model_dimensions", VISUAL_DIMENSIONS)))
        return {**payload, "embedding": vector}

    def health(self) -> dict[str, object]:
        """读取 Compose 视觉服务的真实健康配置，不检查 API 容器内的权重路径。"""
        configured = getattr(self.settings, "visual_health_url", None)
        if not configured:
            # Host 模式仍可使用原有视觉地址推导；Compose 必须显式注入服务 DNS。
            configured = str(getattr(self.settings, "visual_internal_url", "")).replace("/internal/visual-embedding", "/health")
        if not configured:
            return {"status": "degraded", "available": False, "error": "visual_model_not_configured"}
        request = urllib.request.Request(str(configured), headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener(request, timeout=min(10, int(getattr(self.settings, "visual_request_timeout_seconds", 120)))) as response:
                payload = json.loads(response.read(32 * 1024).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return {"status": "degraded", "available": False, "error": "visual_service_unavailable"}
        if not isinstance(payload, dict):
            return {"status": "degraded", "available": False, "error": "visual_service_invalid_response"}
        expected_model = str(getattr(self.settings, "visual_model", VISUAL_MODEL_ID))
        expected_dimensions = int(getattr(self.settings, "visual_model_dimensions", VISUAL_DIMENSIONS))
        expected_preprocess = str(getattr(self.settings, "visual_preprocess_version", VISUAL_PREPROCESS_VERSION))
        identity_matches = (
            payload.get("model") == expected_model
            and payload.get("dimensions") == expected_dimensions
            and payload.get("preprocess_version") == expected_preprocess
        )
        service_error = payload.get("error") if isinstance(payload.get("error"), str) else None
        if not identity_matches:
            service_error = "visual_model_identity_mismatch"
        return {
            "status": payload.get("status") if payload.get("status") in {"ok", "degraded"} else "degraded",
            "available": bool(payload.get("available")) and identity_matches,
            "model": payload.get("model"),
            "dimensions": payload.get("dimensions"),
            "preprocess_version": payload.get("preprocess_version"),
            "error": service_error,
        }


class VisualSearchService:
    """从运行中的 Agent 任务推导 scope 并返回受控视觉近邻。

    local 默认值只服务于开源兼容夹具；应用请求使用 scope-bound facade。
    """

    def __init__(self, settings: Any, resources: DatabaseResources, *, scope_id: str | ScopeContext = "local"):
        self.settings = settings
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.identity = identity_from_settings(settings)

    def match(self, *, task_id: str, top_k: int = 20, exclude_self: bool = True, require_storage: bool = False) -> dict[str, object]:
        """仅允许 running 语境任务查询同 scope 候选，并可严格校验文件身份。

        ``require_storage`` 只由任务前置 snapshot 使用；兼容 callback 查询仍可
        跳过暂时不可用的候选，避免改变旧接口的筛选语义。
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise VisualSearchError("invalid_task", "任务标识无效", status_code=404)
        if isinstance(top_k, bool):
            raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选数量无效", status_code=409)
        try:
            top_k = max(1, min(int(top_k), 50))
        except (TypeError, ValueError) as exc:
            raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选数量无效", status_code=409) from exc
        with self.resources.environment(self.scope.scope_id) as environment:
            task = environment.tasks.get(task_id)
            if task is None or task.task_type != "meme_context_generation":
                raise VisualSearchError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
            if task.status != "running" or (task.claim_generation > 0 and (not task.lease_owner or task.lease_expires_at is None or task.lease_expires_at <= utcnow())):
                raise VisualSearchError("task_not_running", "当前任务不可执行视觉匹配", status_code=409)
            payload = dict(task.payload or {})
            meme_id = payload.get("meme_id")
            if not isinstance(meme_id, str):
                raise VisualSearchError("invalid_task", "任务缺少查询图片", status_code=409)
            try:
                raw_dimensions = payload.get("visual_dimensions")
                requested_identity = VisualModelIdentity(
                    model=str(payload.get("visual_model") or self.identity.model),
                    dimensions=int(raw_dimensions or self.identity.dimensions),
                    preprocess_version=str(payload.get("preprocess_version") or self.identity.preprocess_version),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise VisualSearchError("visual_model_identity_mismatch", "任务视觉模型身份无效", status_code=409) from exc
            # 任务身份必须与服务当前模型一致，避免 Agent 用旧 payload 跨空间查询。
            if requested_identity != self.identity:
                raise VisualSearchError("visual_model_identity_mismatch", "任务视觉模型身份已过期", status_code=409)
            query_meme = environment.memes.get(meme_id)
            if query_meme is None:
                raise VisualSearchError("invalid_task", "查询图片不存在", status_code=404)
            target_sha = payload.get("image_sha256")
            if not isinstance(target_sha, str) or target_sha.lower() != str(query_meme.sha256).lower():
                raise VisualSearchError("target_changed", "查询图片内容已变化", status_code=409)
            query_embedding = environment.visual.get(meme_id, model=self.identity.model, preprocess_version=self.identity.preprocess_version, dimensions=self.identity.dimensions)
            if query_embedding is None or str(query_embedding.image_sha256).lower() != str(query_meme.sha256).lower():
                raise VisualSearchError("query_embedding_not_ready", "查询图片视觉向量尚未就绪", status_code=409)
            rows = environment.visual.match(query_embedding.embedding, model=self.identity.model, preprocess_version=self.identity.preprocess_version, dimensions=self.identity.dimensions, limit=top_k, exclude_meme_id=meme_id if exclude_self else None)
            results: list[dict[str, object]] = []
            blob = None
            if require_storage:
                try:
                    blob = self.resources.blob_store_for_scope(self.scope.scope_id)
                except (DatabaseError, OSError) as exc:
                    raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选存储不可用", status_code=409) from exc
            for _rank, (_embedding, meme, score) in enumerate(rows, start=1):
                storage_key = str(meme.storage_key)
                try:
                    blob = blob or self.resources.blob_store_for_scope(self.scope.scope_id)
                    path = blob.resolve(storage_key)
                    if not blob.exists_with_identity(storage_key, sha256=meme.sha256, size_bytes=meme.size_bytes):
                        if require_storage:
                            raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选图片身份不一致", status_code=409)
                        continue
                except VisualSearchError:
                    raise
                except (DatabaseError, OSError) as exc:
                    if require_storage:
                        raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选图片无法读取", status_code=409) from exc
                    continue
                results.append(
                    {
                        "rank": len(results) + 1,
                        "score": float(score),
                        "meme_id": str(meme.id),
                        "image_path": "/images/" + storage_key,
                        "media_url": f"/media/{meme.id}",
                        "context": copy.deepcopy(meme.meme_context or {}),
                    }
                )
            return {"query_meme_id": meme_id, **self.identity.as_dict(), "results": results}

    def precompute_snapshot(self, *, task_id: str, top_k: int | None = None) -> dict[str, object]:
        """为 Agent 任务生成固定视觉候选 snapshot，不接受 Agent 请求参数。

        输入是已 claim 的语境任务标识和仅由服务端配置决定的候选上限；输出是可写入
        Task JSONB 的 protocol v2 snapshot。候选图片的存储身份在捕获时再次校验，
        任何不一致都以稳定错误阻止后续外部执行。
        """
        configured_limit = getattr(self.settings, "visual_match_top_k", 20)
        try:
            limit = int(configured_limit if top_k is None else top_k)
        except (TypeError, ValueError) as exc:
            raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选数量配置无效", status_code=503) from exc
        limit = max(1, min(limit, 50))
        result = self.match(task_id=task_id, top_k=limit, exclude_self=True, require_storage=True)
        query_meme_id = result.get("query_meme_id")
        if not isinstance(query_meme_id, str):
            raise VisualSearchError("visual_match_snapshot_invalid", "视觉任务缺少查询图片", status_code=409)
        with self.resources.environment(self.scope.scope_id) as environment:
            query_meme = environment.memes.get(query_meme_id)
            if query_meme is None:
                raise VisualSearchError("invalid_task", "查询图片不存在", status_code=404)
            task = environment.tasks.get(task_id)
            if task is None or task.task_type != "meme_context_generation":
                raise VisualSearchError("invalid_task", "任务不存在或不是语境生成任务", status_code=404)
            target_sha = query_meme.sha256
            requested_sha = (task.payload or {}).get("image_sha256")
            if not isinstance(requested_sha, str) or requested_sha.lower() != str(target_sha).lower():
                raise VisualSearchError("target_changed", "查询图片内容已变化", status_code=409)
            try:
                blob = self.resources.blob_store_for_scope(self.scope.scope_id)
                if not blob.exists_with_identity(query_meme.storage_key, sha256=query_meme.sha256, size_bytes=query_meme.size_bytes):
                    raise VisualSearchError("target_changed", "查询图片内容已变化", status_code=409)
            except (DatabaseError, OSError) as exc:
                raise VisualSearchError("target_changed", "查询图片无法读取", status_code=409) from exc
            candidates: list[dict[str, object]] = []
            raw_results = result.get("results")
            if not isinstance(raw_results, list):
                raise VisualSearchError("visual_match_snapshot_invalid", "视觉匹配结果格式无效", status_code=503)
            for item in raw_results:
                if not isinstance(item, Mapping):
                    raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选格式无效", status_code=503)
                meme_id = item.get("meme_id")
                if not isinstance(meme_id, str):
                    raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选缺少图片标识", status_code=503)
                meme = environment.memes.get(meme_id)
                if meme is None or not blob.exists_with_identity(meme.storage_key, sha256=meme.sha256, size_bytes=meme.size_bytes):
                    raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选图片无法校验", status_code=409)
                # match 返回的 score 可能跨越一个并发向量更新；重新读取同一模型空间
                # 和图片 SHA，避免把旧向量的排序分数绑定到新图片/新空间。
                current_embedding = environment.visual.get(
                    meme_id,
                    model=str(result.get("model")),
                    preprocess_version=str(result.get("preprocess_version")),
                    dimensions=result.get("dimensions"),
                    image_sha256=meme.sha256,
                )
                if (
                    current_embedding is None
                    or current_embedding.embedding is None
                    or str(current_embedding.image_sha256).lower() != str(meme.sha256).lower()
                ):
                    raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选向量身份已变化", status_code=409)
                suffix = str(meme.extension or ".bin").lower()
                if not suffix.startswith(".") or len(suffix) > 16 or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in suffix[1:]):
                    raise VisualSearchError("visual_candidate_materialization_failed", "视觉候选扩展名无效", status_code=409)
                candidates.append(
                    {
                        "meme_id": meme_id,
                        "image_sha256": meme.sha256,
                        "size_bytes": int(meme.size_bytes),
                        "score": item.get("score"),
                        "relative_path": f"candidate-{len(candidates) + 1:02d}{suffix}",
                        "context": copy.deepcopy(meme.meme_context or {}),
                    }
                )
            # 匹配结果和 snapshot 生成之间可能发生图片更新；再次读取目标行，
            # 避免把旧向量结果绑定到新图片 SHA。
            latest_query = environment.memes.get(query_meme_id)
            if latest_query is None or str(latest_query.sha256).lower() != str(target_sha).lower() or latest_query.size_bytes != query_meme.size_bytes:
                raise VisualSearchError("target_changed", "查询图片内容已变化", status_code=409)
            try:
                return build_visual_match_snapshot(
                    query_meme_id=query_meme_id,
                    image_sha256=target_sha,
                    model=result.get("model"),
                    dimensions=result.get("dimensions"),
                    preprocess_version=result.get("preprocess_version"),
                    candidates=candidates,
                    matched_at=utcnow(),
                )
            except ValueError as exc:
                raise VisualSearchError("visual_match_snapshot_invalid", "视觉候选 snapshot 无法生成", status_code=503) from exc
