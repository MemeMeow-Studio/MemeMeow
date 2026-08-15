"""隔离的 CPU DINOv2 视觉推理服务入口。

该进程只读取图片和只读模型权重，不连接 PostgreSQL、不读取任务表，也不向宿主发布
端口；主后端通过 Compose 内部网络调用其受控 embedding 接口。
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from backend.visual import (
    VISUAL_CHECKPOINT_FILENAME,
    VISUAL_DIMENSIONS,
    VISUAL_MODEL_ID,
    VISUAL_PREPROCESS_VERSION,
    VisualEmbeddingError,
    VisualModelRunner,
)


def _env(name: str, default: str = "") -> str:
    """读取视觉服务配置；不读取数据库 URL 或 Agent 凭据。"""
    return os.getenv(name, default).strip()


def _service_settings() -> SimpleNamespace:
    """构造模型适配器所需的最小服务端配置对象。"""
    raw_dimensions = _env("MEMEMEOW_VISUAL_DIMENSIONS", str(VISUAL_DIMENSIONS))
    raw_threads = _env("MEMEMEOW_VISUAL_CPU_THREADS", "4")
    raw_interop = _env("MEMEMEOW_VISUAL_CPU_INTEROP_THREADS", "1")
    raw_pixels = _env("MEMEMEOW_VISUAL_MAX_PIXELS", "25000000")
    return SimpleNamespace(
        visual_model=_env("MEMEMEOW_VISUAL_MODEL", VISUAL_MODEL_ID),
        visual_model_dimensions=int(raw_dimensions or VISUAL_DIMENSIONS),
        visual_preprocess_version=_env("MEMEMEOW_VISUAL_PREPROCESS_VERSION", VISUAL_PREPROCESS_VERSION),
        visual_model_repo=Path(_env("MEMEMEOW_VISUAL_MODEL_REPO", "/opt/dinov2")),
        visual_weights_path=Path(
            _env(
                "MEMEMEOW_VISUAL_WEIGHTS_PATH",
                f"/models/{VISUAL_CHECKPOINT_FILENAME}",
            )
        ),
        visual_weights_sha256=_env("MEMEMEOW_VISUAL_WEIGHTS_SHA256") or None,
        visual_cpu_threads=int(raw_threads or 4),
        visual_cpu_interop_threads=int(raw_interop or 1),
        visual_max_pixels=int(raw_pixels or 25_000_000),
    )


settings = _service_settings()
runner = VisualModelRunner(settings)
app = FastAPI(title="MemeMeow Visual Inference", version="1.0.0")


def _check_token(value: str | None) -> None:
    """在部署配置 token 时拒绝非主后端调用。"""
    expected = _env("MEMEMEOW_VISUAL_INTERNAL_TOKEN")
    if expected and (not value or not hmac.compare_digest(value, expected)):
        raise HTTPException(status_code=403, detail={"error": "visual_internal_forbidden", "message": "视觉接口未授权"})


@app.get("/health")
async def health() -> dict[str, object]:
    """返回脱敏模型健康状态，权重路径和凭据永不出现在响应中。"""
    return runner.health()


@app.post("/internal/visual-embedding")
async def visual_embedding(
    image: UploadFile = File(...),
    x_mememeow_internal_token: str | None = Header(default=None),
) -> dict[str, object]:
    """处理单张静态图或 GIF 首帧并返回固定模型空间向量。"""
    _check_token(x_mememeow_internal_token)
    content = await image.read()
    try:
        return runner.embed(content)
    except VisualEmbeddingError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": str(exc)})


@app.exception_handler(HTTPException)
async def http_error_handler(_request, exc: HTTPException) -> JSONResponse:
    """保持视觉服务错误响应为稳定 JSON。"""
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": "visual_http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)
