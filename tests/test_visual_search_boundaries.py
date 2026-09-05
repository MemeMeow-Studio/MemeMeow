"""视觉能力边界的公共 HTTP 契约测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
from backend.image_stage_http import ImageStageSubmissionRequest, submit_image_stage
from backend.callbacks import DEFAULT_CALLBACK_REGISTRY
from backend.operation_policy import Operations


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与应用入口一致的稳定错误。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def test_deleted_visual_routes_are_not_registered() -> None:
    """旧视觉向量和 Agent 视觉 callback 地址必须直接变成 404。"""
    paths = {getattr(route, "path", None) for route in api.app.routes}
    assert "/images/visual-embedding" not in paths
    assert "/images/visual-embedding/batch" not in paths
    assert "/internal/visual-search/match" not in paths
    assert DEFAULT_CALLBACK_REGISTRY.get("/internal/visual-search/match") is None
    assert "analysis.visual_search" not in Operations.ALL


def test_visual_stage_request_does_not_have_a_default_network_policy() -> None:
    """独立视觉阶段省略联网策略，避免把视觉任务误当成联网任务。"""
    request = ImageStageSubmissionRequest.model_validate({"meme_id": "meme-1", "stage": "visual"})
    assert request.reverse_image_policy is None


def test_visual_stage_rejects_network_policy_before_worker_submission() -> None:
    """视觉阶段携带联网策略时，在创建 Task 前返回稳定参数错误。"""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    payload = ImageStageSubmissionRequest.model_validate(
        {"meme_id": "meme-1", "stage": "visual", "reverse_image_policy": "forbid"}
    )
    calls: list[str] = []

    def service(_request: object, name: str) -> object:
        """提供最小当前 scope 元数据服务。"""
        assert name == "metadata"
        calls.append("metadata")
        return SimpleNamespace(image_for_meme=lambda _meme_id: (SimpleNamespace(id="meme-1"), Path("/tmp/image.png")))

    def worker_factory(_request: object) -> object:
        """记录 Worker 是否被错误调用。"""
        calls.append("worker")
        return SimpleNamespace()

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            submit_image_stage(
                request,
                payload,
                service=service,
                error=_error,
                processing_worker=worker_factory,
                normalize_processing_options=lambda *_args, **_kwargs: SimpleNamespace(reverse_image_policy="forbid"),
                processing_config=lambda _request: {},
                task_summary=lambda _request, _task: {},
            )
        )
    assert captured.value.status_code == 400
    assert captured.value.detail["error"] == "invalid_visual_stage_parameter"
    assert calls == []
