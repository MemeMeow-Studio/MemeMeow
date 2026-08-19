"""FastAPI 公共契约、文件边界和上传行为测试。"""

from __future__ import annotations

import io
import json
import zipfile
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, text

from api import app
from backend.collection_packages import CollectionManifest, CollectionManifestCollection, CollectionManifestMember, serialize_manifest, sha256_bytes
from backend.database import EMBEDDING_DIMENSIONS, ImageProcessingJob, Meme, MemeTextEmbedding, StorageOperation, Task, create_engine_for_url
from backend.image_processing import ImageProcessingWorker


def _clear_test_scope() -> None:
    """清理 API 测试使用的 local scope 业务行，保留安装标记和 scope 命名空间。"""
    url = os.getenv("MEMEMEOW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，拒绝清理开发数据库")
    engine = create_engine_for_url(url, pool_size=1, max_overflow=0)
    statements = (
        "DELETE FROM agent_callback_requests",
        "DELETE FROM reverse_image_usage_events",
        "DELETE FROM operation_grants",
        "DELETE FROM image_processing_attempts",
        "DELETE FROM image_processing_stages",
        "DELETE FROM image_processing_jobs",
        "DELETE FROM search_migration_states",
        "DELETE FROM task_lane_slots",
        "DELETE FROM task_batch_items",
        "DELETE FROM task_batches",
        "DELETE FROM tasks",
        "DELETE FROM meme_visual_embeddings",
        "DELETE FROM meme_text_embeddings",
        "DELETE FROM meme_embeddings",
        "DELETE FROM search_heads",
        "DELETE FROM search_generations",
        "DELETE FROM storage_operations",
        "DELETE FROM memes",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """使用隔离图片目录启动完整应用生命周期。"""
    test_url = os.getenv("MEMEMEOW_TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，跳过 API PostgreSQL 测试")
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", test_url)
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    _clear_test_scope()
    with TestClient(app) as test_client:
        uploaded = test_client.post("/images/upload", files=[("files", ("deleted.png", png_bytes(), "image/png"))]).json()["results"][0]

        def fake_embed(content, *, filename="image"):
            """模拟视觉服务，确保夹具直接进入 Agent 阶段而不触发真实模型。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_embed)

        def fake_run(path, progress, *, task_id=None, **kwargs):
            """模拟 Agent 完成前目标图片被删除。"""
            del progress, kwargs
            assert task_id
            path.unlink()
            return {}, "deleted-session"

        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_run)
        response = test_client.post("/images/context", json={"meme_id": uploaded["meme_id"]})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = test_client.get(f"/tasks/{task_id}").json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert status["status"] == "failed"
        assert status["error"]["error"] == "target_changed"
    _clear_test_scope()
    with TestClient(app) as test_client:
        yield test_client, tmp_path
    _clear_test_scope()


def png_bytes(color: str = "red") -> bytes:
    """生成可解码的小型 PNG 测试数据。"""
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=color).save(output, format="PNG")
    return output.getvalue()


def collection_zip_bytes() -> bytes:
    """生成包含一张图片的最小合集包，验证导入任务衔接。"""
    content = png_bytes("green")
    source_id = uuid4().hex
    member = CollectionManifestMember(
        source_meme_id=source_id,
        filename_at_export="imported.png",
        path=f"images/{source_id}.png",
        extension=".png",
        size_bytes=len(content),
        sha256=sha256_bytes(content),
    )
    manifest = CollectionManifest(
        format="mememeow-collection",
        format_version=1,
        collection=CollectionManifestCollection(name=f"导入测试-{source_id[:8]}"),
        members=[member],
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", serialize_manifest(manifest))
        archive.writestr(member.path, content)
    return output.getvalue()


def test_config_is_masked_and_search_requires_cache(client):
    """配置响应不泄露密钥，缓存未就绪时搜索返回规范错误。"""
    test_client, _ = client
    config = test_client.get("/config")
    assert config.status_code == 200
    assert "embedding_api_key" not in config.json()
    assert "opencode_executable" not in config.json()
    assert config.json()["embedding_api_key_configured"] is False
    assert config.json()["embedding_cache_ready"] is False
    response = test_client.post("/search", json={"query": "开心"})
    assert response.status_code == 503
    assert response.json()["error"] == "cache_not_ready"


def test_startup_allows_root_orphan_files_without_listing_them(tmp_path, monkeypatch):
    """启动预检允许根目录孤立图片，并继续只从数据库列出可信 Meme。"""
    test_url = os.getenv("MEMEMEOW_TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，跳过 API PostgreSQL 测试")
    image_root = tmp_path / "images"
    image_root.mkdir(parents=True)
    orphan = image_root / "legacy.png"
    orphan.write_bytes(png_bytes())
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", test_url)
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(image_root))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    _clear_test_scope()
    with TestClient(app) as test_client:
        health = test_client.get("/health")
        assert health.status_code == 200
        assert health.json()["storage_preflight"] == {"status": "warning", "orphan_files": 1, "blocking_errors": {}}
        assert test_client.get("/config").json()["storage_preflight"] == health.json()["storage_preflight"]
        assert test_client.get("/images").json()["total"] == 0
        assert orphan.is_file()
    _clear_test_scope()


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "x", "n_results": 0}, {"query": "x", "n_results": 31}, {"query": "x", "n_results": "5"}])
def test_search_validation_is_consistent(client, payload):
    """空查询和越界数量统一返回 400。"""
    test_client, _ = client
    response = test_client.post("/search", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_get_search_is_not_an_alias(client):
    """旧 GET 检索入口不会执行检索逻辑。"""
    test_client, _ = client
    assert test_client.get("/search", params={"q": "x"}).status_code == 405


def test_upload_lists_and_serves_image(client):
    """上传后的图片可立即列出并通过受控媒体接口读取。"""
    test_client, _ = client
    response = test_client.post("/images/upload", files=[("files", ("hello.png", png_bytes(), "image/png"))])
    assert response.status_code == 200
    uploaded = response.json()["results"][0]
    assert uploaded["ok"] is True
    meme_id = uploaded["meme_id"]
    UUID(meme_id)
    listing = test_client.get("/images").json()
    assert listing["total"] == 1
    media_url = listing["items"][0]["media_url"]
    media = test_client.get(media_url)
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"
    listed = test_client.get("/images").json()["items"][0]
    assert listed["meme_id"] == meme_id
    assert listed["media_url"] == f"/media/{meme_id}"
    assert listed["metadata"]["status"] == "pending"
    assert listed["embedding_status"] == "pending"


def test_upload_rejects_more_than_twenty_files_before_durable_write(client):
    """超过每请求文件数边界时在任何 Meme 或 operation 写入前拒绝。"""
    test_client, _ = client
    response = test_client.post(
        "/images/upload",
        files=[("files", (f"too-many-{index}.png", png_bytes(), "image/png")) for index in range(21)],
    )
    assert response.status_code == 413
    assert response.json()["error"] == "too_many_files"
    assert test_client.get("/images").json()["total"] == 0


def test_upload_accepts_twenty_files_and_exposes_default_limits(client):
    """恰好 20 个文件可以逐项入库，配置响应保持默认 20/2/disabled。"""
    test_client, _ = client
    response = test_client.post(
        "/images/upload",
        files=[("files", (f"limit-{index}.png", png_bytes(), "image/png")) for index in range(20)],
    )
    assert response.status_code == 200
    assert len(response.json()["results"]) == 20
    assert all(item["ok"] for item in response.json()["results"])
    config = test_client.get("/config").json()
    assert config["max_files_per_request"] == 20
    assert config["max_concurrent_upload_requests"] == 2
    assert config["max_request_bytes"] is None


def test_upload_optional_total_budget_is_enforced_without_content_length_dependency(client):
    """启用总预算后按实际读取字节拒绝超限请求，而不是信任 Content-Length。"""
    test_client, _ = client
    settings = test_client.app.state.settings
    previous = settings.max_request_bytes
    settings.max_request_bytes = len(png_bytes())
    try:
        response = test_client.post(
            "/images/upload",
            files=[
                ("files", ("budget-a.png", png_bytes("red"), "image/png")),
                ("files", ("budget-b.png", png_bytes("blue"), "image/png")),
            ],
        )
    finally:
        settings.max_request_bytes = previous
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert test_client.get("/images").json()["total"] == 0


def test_upload_retry_reuses_durable_meme_operation_and_processing_job(client):
    """响应丢失后的同内容重试只返回原 Meme 和处理 job，不重复 durable 事实。"""
    test_client, _ = client
    content = png_bytes("red")
    first = test_client.post("/images/upload", files=[("files", ("idempotent.png", content, "image/png"))])
    assert first.status_code == 200
    first_item = first.json()["results"][0]
    engine = test_client.app.state.database.engine
    with engine.connect() as connection:
        before = (
            connection.execute(select(Meme).where(Meme.scope_id == "local")).all(),
            connection.execute(select(StorageOperation).where(StorageOperation.scope_id == "local")).all(),
            connection.execute(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == "local")).all(),
        )
    second = test_client.post("/images/upload", files=[("files", ("idempotent.png", content, "image/png"))])
    assert second.status_code == 200
    second_item = second.json()["results"][0]
    assert second_item["ok"] is True
    assert second_item["idempotent"] is True
    assert second_item["meme_id"] == first_item["meme_id"]
    with engine.connect() as connection:
        after = (
            connection.execute(select(Meme).where(Meme.scope_id == "local")).all(),
            connection.execute(select(StorageOperation).where(StorageOperation.scope_id == "local")).all(),
            connection.execute(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == "local")).all(),
        )
    assert tuple(len(items) for items in after) == tuple(len(items) for items in before)


def test_upload_retry_reports_reconciliation_when_file_fact_changes(client):
    """数据库记录与实际文件指纹不一致时不盲目认领上传。"""
    test_client, tmp_path = client
    content = png_bytes("red")
    first = test_client.post("/images/upload", files=[("files", ("reconcile.png", content, "image/png"))])
    assert first.status_code == 200
    (tmp_path / "images" / "reconcile.png").write_bytes(png_bytes("blue"))
    response = test_client.post("/images/upload", files=[("files", ("reconcile.png", content, "image/png"))])
    assert response.status_code == 200
    assert response.json()["results"][0]["error"] == "upload_reconciliation_required"


def test_multi_upload_seals_visual_batch(client):
    """多图上传必须把视觉任务登记到批次后再封口，不能返回 batch_not_found。"""
    test_client, _ = client
    response = test_client.post(
        "/images/upload",
        files=[
            ("files", ("batch-a.png", png_bytes("red"), "image/png")),
            ("files", ("batch-b.png", png_bytes("blue"), "image/png")),
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"]
    assert all(item["ok"] and item["visual_task_id"] for item in payload["results"])


def test_image_metadata_returns_complete_database_record(client):
    """图片库详情接口只返回受控 Meme 对应的完整数据库元数据。"""
    test_client, tmp_path = client
    response = test_client.post("/images/upload", files=[("files", ("detail.png", png_bytes(), "image/png"))])
    assert response.status_code == 200
    meme_id = response.json()["results"][0]["meme_id"]
    image = tmp_path / "images" / "detail.png"
    test_client.app.state.metadata.update_context(image, {"summary": "详情摘要", "keywords": ["测试"]}, producer="human", status="ready")

    metadata = test_client.get("/images/metadata", params={"meme_id": meme_id})
    assert metadata.status_code == 200
    payload = metadata.json()
    assert payload["image"]["relative_path"] == "detail.png"
    assert payload["context_status"] == "ready"
    assert payload["meme_context"]["summary"] == "详情摘要"
    assert payload["meme_context"]["keywords"] == ["测试"]


def test_image_metadata_requires_stable_id_and_reports_unknown_meme(client):
    """详情接口拒绝旧路径参数，并对未知 Meme 返回稳定错误。"""
    test_client, _ = client
    assert test_client.get("/images/metadata", params={"filename": "missing.png"}).json()["error"] == "meme_id_required"
    response = test_client.get("/images/metadata", params={"meme_id": str(uuid4())})
    assert response.status_code == 404
    assert response.json()["error"] == "meme_not_found"


def test_batch_upload_keeps_success_and_reports_failure(client):
    """批量上传允许部分成功且不覆盖重复文件。"""
    test_client, _ = client
    response = test_client.post(
        "/images/upload",
        files=[
            ("files", ("good.png", png_bytes(), "image/png")),
            ("files", ("bad.png", b"not-an-image", "image/png")),
            ("files", ("note.txt", b"text", "text/plain")),
        ],
    )
    results = response.json()["results"]
    assert [item["ok"] for item in results] == [True, False, False]
    duplicate = test_client.post("/images/upload", files=[("files", ("good.png", png_bytes("blue"), "image/png"))])
    assert duplicate.json()["results"][0]["error"] == "file_exists"


def test_upload_queues_context_job_and_removed_vlm_routes_are_not_found(client):
    """上传创建异步语境任务，原 VLM 路由不再暴露。"""
    test_client, _ = client
    response = test_client.post("/images/upload", files=[("files", ("pending.png", png_bytes(), "image/png"))])
    item = response.json()["results"][0]
    assert item["metadata_job_id"]
    assert test_client.get(f"/tasks/{item['metadata_job_id']}").status_code == 200
    assert test_client.post("/images/describe", json={"filename": "pending.png"}).status_code == 404
    assert test_client.post("/images/label-batch", json={"items": []}).status_code == 404


def test_collection_import_starts_with_visual_task(client):
    """合集导入后的新 Meme 必须先进入视觉任务，而不是直接进入 Agent。"""
    test_client, _ = client
    response = test_client.post("/collections/import", files={"file": ("import.zip", collection_zip_bytes(), "application/zip")})
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["ok"] is True
    assert item["visual_task_id"] == item["metadata_job_id"]
    task = test_client.get(f"/tasks/{item['visual_task_id']}")
    assert task.status_code == 200
    assert task.json()["task_type"] == "visual_embedding_generation"


def test_removed_directory_contract_and_rename_conflicts(client):
    """目录接口永久删除，重命名仍拒绝重复目标。"""
    test_client, tmp_path = client
    assert test_client.get("/images/directories").status_code == 404
    assert test_client.post("/images/directories", json={"name": "work"}).status_code == 404
    one = test_client.post("/images/upload", files=[("files", ("one.png", png_bytes(), "image/png"))]).json()["results"][0]
    two = test_client.post("/images/upload", files=[("files", ("two.png", png_bytes("blue"), "image/png"))]).json()["results"][0]
    conflict = test_client.post("/images/rename", json={"meme_id": one["meme_id"], "new_name": "two"})
    assert conflict.status_code == 409
    renamed = test_client.post("/images/rename", json={"meme_id": one["meme_id"], "new_name": "first"})
    assert renamed.status_code == 200
    assert renamed.json()["meme_id"] == one["meme_id"]
    assert renamed.json()["filename"] == "first.png"
    assert test_client.get(f"/media/{one['meme_id']}").status_code == 200
    assert test_client.get(f"/media/{two['meme_id']}").status_code == 200


def test_delete_removes_image_and_database_record(client):
    """删除接口会移除数据库 Meme、文件和受控媒体访问。"""
    test_client, tmp_path = client
    uploaded = test_client.post("/images/upload", files=[("files", ("remove.png", png_bytes(), "image/png"))]).json()["results"][0]
    meme_id = uploaded["meme_id"]
    response = test_client.post("/images/delete", json={"meme_id": meme_id})
    assert response.status_code == 200
    assert not (tmp_path / "images" / "remove.png").exists()
    assert test_client.get(f"/media/{meme_id}").status_code == 404
    assert test_client.get("/images").json()["total"] == 0


def test_metadata_repair_is_pollable(client):
    """元数据修复任务报告数据库与文件完整性问题。"""
    test_client, tmp_path = client
    image = tmp_path / "images" / "legacy.png"
    image.write_bytes(png_bytes())
    response = test_client.post("/images/metadata/repair")
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    for _ in range(50):
        status = test_client.get(f"/tasks/{task_id}").json()
        if status["status"] in {"succeeded", "failed"}:
            break
    assert status["status"] == "succeeded"
    assert status["result"]["processed"] == 0
    assert "legacy.png" in status["result"]["orphan_files"]
    second = test_client.post("/images/metadata/repair")
    assert second.status_code == 202


def test_path_traversal_and_symlink_escape_are_blocked(client):
    """目录穿越和指向根目录外的符号链接均不可读取。"""
    test_client, tmp_path = client
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(png_bytes())
    (tmp_path / "images" / "escape").symlink_to(outside, target_is_directory=True)
    traversal = test_client.get("/images", params={"directory": "../outside"})
    assert traversal.status_code == 400
    escaped = test_client.get("/media/escape/secret.png")
    assert escaped.status_code == 404


def test_unknown_task_has_stable_error(client):
    """未知或重启后丢失的任务返回 task_not_found。"""
    test_client, _ = client
    response = test_client.get("/tasks/unknown")
    assert response.status_code == 404
    assert response.json()["error"] == "task_not_found"


def test_task_list_filters_and_does_not_expose_payload(client):
    """持久任务列表支持类型筛选且不会返回内部 payload。"""
    test_client, _ = client
    submitted = test_client.post("/generate-cache")
    assert submitted.status_code == 202
    response = test_client.get("/tasks", params={"task_type": "cache_generation", "limit": 1})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["task_type"] == "cache_generation"
    assert "payload" not in items[0]


def test_search_maps_results_to_media_urls(client):
    """正常检索只返回受控媒体 URL 且不重复。"""
    test_client, _ = client
    uploaded = test_client.post("/images/upload", files=[("files", ("a.png", png_bytes(), "image/png"))]).json()["results"][0]
    meme_id = uploaded["meme_id"]

    class FakeSearch:
        def has_cache(self):
            return True

        def search(self, *args, **kwargs):
            return [meme_id, meme_id, str(uuid4())]

    test_client.app.state.search_engine = FakeSearch()
    response = test_client.post("/search", json={"query": "x", "n_results": 3})
    assert response.status_code == 200
    assert response.json() == {"results": [f"/media/{meme_id}"]}


def test_llm_failure_falls_back_to_original_query(client):
    """LLM 增强异常时仍执行一次普通检索。"""
    test_client, _ = client
    meme_id = test_client.post("/images/upload", files=[("files", ("a.png", png_bytes(), "image/png"))]).json()["results"][0]["meme_id"]
    calls = []

    class FakeSearch:
        def has_cache(self):
            return True

        def search(self, query, *args, **kwargs):
            calls.append((query, kwargs.get("use_llm")))
            if kwargs.get("use_llm"):
                raise RuntimeError("llm unavailable")
            return [meme_id]

    test_client.app.state.search_engine = FakeSearch()
    response = test_client.post("/search", json={"query": "原始查询", "llm_enhance": True})
    assert response.status_code == 200
    assert calls == [("原始查询", True), ("原始查询", False)]


def test_cache_generation_returns_pollable_task(client):
    """缓存生成通过 202 和任务状态接口返回结果。"""
    test_client, _ = client

    class FakeSearch:
        def generate_cache(self, progress):
            progress(1.0, "done")

        def has_cache(self):
            return False

    test_client.app.state.search_engine = FakeSearch()
    response = test_client.post("/generate-cache")
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    for _ in range(50):
        status = test_client.get(f"/tasks/{task_id}").json()
        if status["status"] in {"succeeded", "failed"}:
            break
    assert status["status"] == "succeeded"


def test_parallel_context_batch_writes_independent_database_records_and_incremental_vectors(tmp_path, monkeypatch):
    """并发度为 2 时两张图片独立完成逐图文本向量，且不隐式创建全库缓存任务。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "2")
    _clear_test_scope()
    with TestClient(app) as test_client:
        first = test_client.post("/images/upload", files=[("files", ("parallel-a.png", png_bytes("red"), "image/png"))]).json()["results"][0]
        second = test_client.post("/images/upload", files=[("files", ("parallel-b.png", png_bytes("blue"), "image/png"))]).json()["results"][0]
        started = threading.Barrier(2)
        sessions: list[str] = []

        def fake_embed(content, *, filename="image"):
            """模拟视觉服务，使并发断言只覆盖任务调度和 Agent 写回。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_embed)

        def fake_text_embedding(text):
            """模拟文本 embedding，使统一 job 可以完成最后阶段。"""
            del text
            return [1.0] + [0.0] * 1023

        monkeypatch.setattr(test_client.app.state.search_engine, "_embedding", fake_text_embedding)

        def fake_run(image, progress, *, task_id=None, **kwargs):
            """模拟两个独立 Agent session，并在 barrier 处确认真正并行。"""
            del progress, kwargs
            assert task_id
            sessions.append(image.name)
            started.wait(timeout=2)
            return {
                "title": image.stem.replace("-", " "),
                "summary": "并发测试",
                "subjects": [image.name],
                "visible_text": [],
                "references": [],
                "meaning": None,
                "keywords": ["测试"],
                "search_queries": [],
                "uncertainties": [],
                "source_urls": [],
            }, f"session-{image.stem}"

        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_run)
        submitted = test_client.post("/images/context/batch", json={"items": [{"meme_id": first["meme_id"]}, {"meme_id": second["meme_id"]}], "include_unready": True})
        assert submitted.status_code == 200
        task_ids = [item["task_id"] for item in submitted.json()["results"]]
        assert len(task_ids) == 2
        deadline = time.monotonic() + 5
        while len(sessions) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(sessions) == {"parallel-a.png", "parallel-b.png"}
        for task_id in task_ids:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                record = test_client.get(f"/tasks/{task_id}").json()
                if record["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
        assert record["status"] == "succeeded"
        assert test_client.get(f"/images/metadata?meme_id={first['meme_id']}").json()["context_status"] == "ready"
        assert test_client.get(f"/images/metadata?meme_id={second['meme_id']}").json()["context_status"] == "ready"
        text_items = test_client.get("/tasks", params={"task_type": "text_embedding_generation"}).json()["items"]
        assert len(text_items) == 2
        assert all(item["status"] == "succeeded" for item in text_items)
        cache_items = test_client.get("/tasks", params={"task_type": "cache_generation"}).json()["items"]
        assert cache_items == []
    _clear_test_scope()


def test_pending_agent_ready_sidecar_and_v4_embedding_pipeline_is_backend_owned(tmp_path, monkeypatch):
    """测试图片从 pending 经 Agent 到 ready/v4 索引，canonical 写回始终由后端掌控。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "mememeow/gpt-5.6-luna")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "1")
    _clear_test_scope()


    application_file = Path(__file__).resolve().parent.parent / "api.py"
    application_before = application_file.read_bytes()
    with TestClient(app) as test_client:
        def fake_visual_embed(content, *, filename="image"):
            """模拟视觉服务，使测试只验证逐图任务衔接。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        def fake_text_embedding(text):
            """模拟 v4/逐图文本 embedding 服务并保留固定维度。"""
            assert text
            return [1.0] + [0.0] * 1023

        def fake_agent_run(image, progress, *, task_id=None, callback_token=None, **kwargs):
            """模拟 Agent 只交付任务 artifact，拒绝把伪造字段写入 canonical 数据。"""
            del progress, kwargs
            assert task_id and callback_token
            assert callback_token != "test-agent-callback-secret-1234"
            binding = test_client.app.state.callback_verifier.verify(callback_token)
            remaining = binding.expires_at - datetime.now(UTC)
            assert timedelta(hours=1, minutes=59) < remaining <= timedelta(hours=2)
            runner = test_client.app.state.opencode
            draft_path, result_path = runner.create_task_result_paths(task_id)
            candidate = {
                "title": image.stem.replace("-", " "),
                "summary": "后端拥有 canonical 写回",
                "subjects": ["测试图片"],
                "visible_text": [],
                "references": [],
                "meaning": None,
                "keywords": ["测试"],
                "search_queries": [],
                "uncertainties": [],
                "source_urls": [],
                "canonical_sidecar": "agent cannot set this",
                "application_code": "agent cannot set this",
            }
            # 运行器只接受任务专属结果文件；canonical sidecar 和应用代码不在
            # Agent artifact 路径中，最终写回由 context_handler 的白名单完成。
            draft_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            draft_path.replace(result_path)
            return runner.read_result_file(result_path), f"session-{image.stem}"

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_visual_embed)
        monkeypatch.setattr(test_client.app.state.search_engine, "_embedding", fake_text_embedding)
        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_agent_run)

        uploaded = test_client.post(
            "/images/upload",
            files=[("files", ("pending-agent.png", png_bytes("purple"), "image/png"))],
        )
        assert uploaded.status_code == 200
        result = uploaded.json()["results"][0]
        meme_id = result["meme_id"]
        assert result["metadata_status"] == "pending"
        assert application_file.read_bytes() == application_before

        processing_id = result["processing_job_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            processing = test_client.get(f"/images/processing/{processing_id}").json()
            if processing["status"] in {"succeeded", "failed", "blocked", "unknown_execution"}:
                break
            time.sleep(0.02)
        assert processing["status"] == "succeeded"
        assert [stage["stage"] for stage in processing["stages"]] == ["visual", "agent", "auto_rename", "text_embedding"]
        assert [stage["status"] for stage in processing["stages"]] == ["succeeded", "succeeded", "skipped", "succeeded"]

        metadata = test_client.get("/images/metadata", params={"meme_id": meme_id})
        assert metadata.status_code == 200
        metadata_payload = metadata.json()
        assert metadata_payload["context_status"] == "ready"
        assert metadata_payload["meme_context"]["title"] == "pending agent"
        assert "canonical_sidecar" not in metadata_payload["meme_context"]
        assert "application_code" not in metadata_payload["meme_context"]
        assert application_file.read_bytes() == application_before

        text_tasks = test_client.get("/tasks", params={"task_type": "text_embedding_generation"}).json()["items"]
        assert len(text_tasks) == 1
        assert text_tasks[0]["status"] == "succeeded"
        cache_submission = test_client.post("/generate-cache")
        assert cache_submission.status_code == 202
        cache_task_id = cache_submission.json()["task_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cache_task = test_client.get(f"/tasks/{cache_task_id}").json()
            if cache_task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert cache_task["status"] == "succeeded"
        assert cache_task["result"]["indexed_count"] == 1
        assert cache_task["result"]["model"] == test_client.app.state.settings.embedding_model
        assert application_file.read_bytes() == application_before
    _clear_test_scope()


def test_auto_name_pipeline_renames_safely_and_freezes_final_metadata_hash(tmp_path, monkeypatch):
    """自动命名四阶段必须按顺序完成，并让文本 Task 绑定重命名后的 metadata hash。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "mememeow/gpt-5.6-luna")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "1")
    _clear_test_scope()
    with TestClient(app) as test_client:
        def fake_visual_embed(content, *, filename="image"):
            """模拟视觉服务，确保测试只验证图片处理控制面。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        def fake_text_embedding(text_value):
            """模拟固定维度文本向量，避免访问外部 embedding 服务。"""
            assert text_value
            return [1.0] + [0.0] * 1023

        def fake_agent_run(image, progress, *, task_id=None, callback_token=None, **kwargs):
            """交付带非法路径字符的标题，验证服务端安全派生文件名。"""
            del progress, callback_token, kwargs
            assert task_id
            runner = test_client.app.state.opencode
            draft_path, result_path = runner.create_task_result_paths(task_id)
            draft_path.write_text(
                json.dumps(
                    {
                        "title": "Project Launch v2",
                        "summary": "自动命名测试",
                        "subjects": ["测试图片"],
                        "visible_text": [],
                        "references": [],
                        "meaning": None,
                        "keywords": ["测试"],
                        "search_queries": [],
                        "uncertainties": [],
                        "source_urls": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            draft_path.replace(result_path)
            return runner.read_result_file(result_path), f"session-{image.stem}"

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_visual_embed)
        monkeypatch.setattr(test_client.app.state.search_engine, "_embedding", fake_text_embedding)
        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_agent_run)

        uploaded = test_client.post(
            "/images/upload",
            data={"auto_name": "true", "reverse_image_policy": "forbid"},
            files=[("files", ("original-name.png", png_bytes("purple"), "image/png"))],
        )
        assert uploaded.status_code == 200
        result = uploaded.json()["results"][0]
        assert result["ok"] is True
        assert result["auto_name"] is True
        assert result["saved_filename"] == "original-name.png"
        job_id = result["processing_job_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            processing = test_client.get(f"/images/processing/{job_id}").json()
            if processing.get("status") in {"succeeded", "failed", "blocked", "unknown_execution"}:
                break
            time.sleep(0.02)
        assert processing["status"] == "succeeded"
        assert processing["auto_name"] is True
        assert [stage["stage"] for stage in processing["stages"]] == ["visual", "agent", "auto_rename", "text_embedding"]
        assert [stage["status"] for stage in processing["stages"]] == ["succeeded", "succeeded", "succeeded", "succeeded"]
        assert processing["has_warnings"] is False

        metadata = test_client.get("/images/metadata", params={"meme_id": result["meme_id"]}).json()
        assert metadata["image"]["relative_path"] == "Project Launch v2.png"
        assert not (tmp_path / "images" / "original-name.png").exists()
        assert (tmp_path / "images" / "Project Launch v2.png").exists()

        with test_client.app.state.database.environment("local") as environment:
            meme = environment.memes.get(result["meme_id"])
            assert meme is not None
            final_hash = ImageProcessingWorker._metadata_hash(meme)
            assert final_hash is not None
            rename_task = environment.tasks.get(
                next(
                    stage["task_id"]
                    for stage in processing["stages"]
                    if stage["stage"] == "auto_rename"
                )
            )
            assert rename_task is not None
            assert rename_task.status == "succeeded"
            assert "auto_name" not in rename_task.payload
            text_task = environment.uow.session.scalar(
                select(Task).where(
                    Task.scope_id == "local",
                    Task.task_type == "text_embedding_generation",
                    Task.processing_job_id == job_id,
                )
            )
            assert text_task is not None
            assert text_task.payload["metadata_hash"] == final_hash
            old_hash_rows = list(
                environment.uow.session.scalars(
                    select(MemeTextEmbedding).where(
                        MemeTextEmbedding.scope_id == "local",
                        MemeTextEmbedding.meme_id == meme.id,
                        MemeTextEmbedding.metadata_hash == final_hash,
                        MemeTextEmbedding.status == "ready",
                    )
                )
            )
            assert len(old_hash_rows) == 1

    _clear_test_scope()


def test_auto_name_warning_keeps_leaf_failure_and_continues_text_stage(tmp_path, monkeypatch):
    """缺少标题时叶子 Task 必须失败为事实，父 Job 以 warning 继续文本阶段。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "mememeow/gpt-5.6-luna")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    _clear_test_scope()


    with TestClient(app) as test_client:
        def fake_visual_embed(content, *, filename="image"):
            """模拟视觉向量服务。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        def fake_text_embedding(text_value):
            """模拟文本向量服务。"""
            assert text_value
            return [1.0] + [0.0] * 1023

        def fake_agent_run(image, progress, *, task_id=None, callback_token=None, **kwargs):
            """交付没有标题但其余字段有效的 Agent 语境。"""
            del image, progress, callback_token, kwargs
            assert task_id
            runner = test_client.app.state.opencode
            draft_path, result_path = runner.create_task_result_paths(task_id)
            draft_path.write_text(
                json.dumps(
                    {
                        "title": None,
                        "summary": "保留原始文件名",
                        "subjects": ["测试图片"],
                        "visible_text": [],
                        "references": [],
                        "meaning": None,
                        "keywords": ["测试"],
                        "search_queries": [],
                        "uncertainties": [],
                        "source_urls": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            draft_path.replace(result_path)
            return runner.read_result_file(result_path), "warning-session"

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_visual_embed)
        monkeypatch.setattr(test_client.app.state.search_engine, "_embedding", fake_text_embedding)
        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_agent_run)
        uploaded = test_client.post(
            "/images/upload",
            data={"auto_name": "true"},
            files=[("files", ("keep-original.png", png_bytes("blue"), "image/png"))],
        )
        assert uploaded.status_code == 200
        result = uploaded.json()["results"][0]
        job_id = result["processing_job_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            processing = test_client.get(f"/images/processing/{job_id}").json()
            if processing.get("status") in {"succeeded", "failed", "blocked", "unknown_execution"}:
                break
            time.sleep(0.02)
        assert processing["status"] == "succeeded"
        assert processing["has_warnings"] is True
        assert processing["warnings"] == [
            {
                "stage": "auto_rename",
                "error": "auto_rename_title_missing",
                "message": "自动重命名未完成",
                "recoverable": True,
            }
        ]
        assert [stage["status"] for stage in processing["stages"]] == ["succeeded", "succeeded", "warning", "succeeded"]
        assert test_client.get("/images/metadata", params={"meme_id": result["meme_id"]}).json()["image"]["relative_path"] == "keep-original.png"
        rename_tasks = test_client.get("/tasks", params={"task_type": "image_auto_rename"}).json()["items"]
        assert len(rename_tasks) == 1
        assert rename_tasks[0]["status"] == "failed"
        assert rename_tasks[0]["error"]["error"] == "auto_rename_title_missing"
        assert rename_tasks[0]["image_stage_recoverable"] is True
        text_tasks = test_client.get("/tasks", params={"task_type": "text_embedding_generation"}).json()["items"]
        assert len(text_tasks) == 1 and text_tasks[0]["status"] == "succeeded"
    _clear_test_scope()


def test_scope_unready_processing_is_option_only_and_fail_closed(client, monkeypatch):
    """scope 级未就绪入口不接受分页筛选，并在能力拒绝前不产生业务副作用。"""
    test_client, _ = client
    # 该测试必须稳定覆盖能力拒绝分支，不能受宿主环境中的 SerpAPI 凭据影响。
    monkeypatch.setattr(test_client.app.state.reverse_image, "_provider_factory", None)
    monkeypatch.setattr(test_client.app.state.reverse_image.settings, "serpapi_api_key", "")

    unavailable = test_client.post(
        "/images/processing/unready",
        json={"reverse_image_policy": "auto", "auto_name": False},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "reverse_image_unavailable"
    with test_client.app.state.database.environment("local") as environment:
        assert environment.uow.session.scalar(select(Meme.id)) is None

    first = test_client.post("/images/upload", files=[("files", ("scope-first.png", png_bytes("red"), "image/png"))]).json()["results"][0]
    second = test_client.post("/images/upload", files=[("files", ("scope-second.png", png_bytes("blue"), "image/png"))]).json()["results"][0]
    invalid_bool = test_client.post(
        "/images/processing/unready",
        json={"reverse_image_policy": "forbid", "auto_name": "true"},
    )
    assert invalid_bool.status_code == 400
    assert invalid_bool.json()["error"] == "invalid_request"

    response = test_client.post(
        "/images/processing/unready",
        json={"reverse_image_policy": "forbid", "auto_name": False},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["target_count"] == 2
    assert {item["meme_id"] for item in body["results"]} == {first["meme_id"], second["meme_id"]}
    assert all(item.get("processing_job_id") or item.get("error") for item in body["results"])

    filtered = test_client.post(
        "/images/processing/unready?page=2",
        json={"reverse_image_policy": "forbid", "auto_name": False},
    )
    assert filtered.status_code == 400
    assert filtered.json()["error"] == "invalid_request"


def test_selected_stage_batch_validates_core_mapping_and_submits_independent_tasks(client):
    """选中重试只接受三个核心阶段，并为每个图片阶段创建独立任务。"""
    test_client, _ = client
    uploaded = test_client.post(
        "/images/upload",
        files=[("files", ("selected-stage.png", png_bytes("green"), "image/png"))],
    ).json()["results"][0]
    meme_id = uploaded["meme_id"]

    invalid = test_client.post(
        "/images/stages/batch",
        json={"items": [{"meme_id": meme_id}], "stages": ["auto_rename"]},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_image_stage"

    response = test_client.post(
        "/images/stages/batch",
        json={"items": [{"meme_id": meme_id}], "stages": ["visual", "text_embedding"]},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["target_count"] == 2
    assert {item["stage"] for item in body["results"]} == {"visual", "text_embedding"}
    assert all(item.get("task_id") or item.get("error") for item in body["results"])


def test_standalone_auto_rename_invalidates_old_text_embedding_without_creating_job(tmp_path, monkeypatch):
    """独立自动重命名成功后必须失效旧向量，且不能创建父 Job 或文本任务。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    _clear_test_scope()
    with TestClient(app) as test_client:
        metadata_service = test_client.app.state.metadata
        meme_id, image = metadata_service.upload_bytes(png_bytes("blue"), target_key="standalone-source.png")
        metadata_service.update_context(
            image,
            {"title": "Standalone renamed", "summary": "独立阶段测试"},
            producer="research",
            model="test-model",
            status="ready",
        )
        with test_client.app.state.database.environment("local") as environment:
            record = environment.memes.get(meme_id)
            assert record is not None
            old_metadata_hash = ImageProcessingWorker._metadata_hash(record)
            assert old_metadata_hash is not None
            old_sha256 = record.sha256
            environment.uow.session.add(
                MemeTextEmbedding(
                    scope_id="local",
                    meme_id=record.id,
                    image_sha256=old_sha256,
                    metadata_hash=old_metadata_hash,
                    embedding_model_version=test_client.app.state.settings.embedding_model,
                    dimensions=EMBEDDING_DIMENSIONS,
                    semantic_document="标题：Standalone renamed",
                    embedding=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
                    status="ready",
                )
            )
            environment.uow.session.commit()

        submitted = test_client.post(
            "/images/stages",
            json={"meme_id": str(meme_id), "stage": "auto_rename"},
        )
        assert submitted.status_code == 202
        task_id = submitted.json()["task_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = test_client.get(f"/tasks/{task_id}").json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert task["status"] == "succeeded"

        with test_client.app.state.database.environment("local") as environment:
            record = environment.memes.get(meme_id)
            assert record is not None
            assert record.storage_key == "Standalone renamed.png"
            row = environment.uow.session.scalar(
                select(MemeTextEmbedding).where(
                    MemeTextEmbedding.scope_id == "local",
                    MemeTextEmbedding.meme_id == record.id,
                    MemeTextEmbedding.image_sha256 == old_sha256,
                    MemeTextEmbedding.metadata_hash == old_metadata_hash,
                )
            )
            assert row is not None
            assert row.status == "failed"
            assert environment.uow.session.scalar(select(Task).where(Task.task_type == "text_embedding_generation")) is None
            assert environment.uow.session.scalar(select(Task).where(Task.processing_job_id.is_not(None))) is None
    _clear_test_scope()


def test_context_target_deleted_during_agent_is_reported_as_target_changed(tmp_path, monkeypatch):
    """Agent 运行期间图片被删除时，任务必须返回稳定的目标变化错误。"""
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", os.getenv("MEMEMEOW_TEST_DATABASE_URL", ""))
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "test-agent-callback-secret-1234")
    _clear_test_scope()
    with TestClient(app) as test_client:
        uploaded = test_client.post("/images/upload", files=[("files", ("deleted.png", png_bytes(), "image/png"))]).json()["results"][0]

        def fake_embed(content, *, filename="image"):
            """模拟视觉服务，避免目标变化测试访问真实视觉模型。"""
            del content, filename
            settings = test_client.app.state.settings
            return {
                "model": settings.visual_model,
                "dimensions": settings.visual_model_dimensions,
                "preprocess_version": settings.visual_preprocess_version,
                "embedding": [1.0] + [0.0] * (settings.visual_model_dimensions - 1),
            }

        monkeypatch.setattr(test_client.app.state.visual_inference, "embed", fake_embed)

        def fake_run(path, progress, *, task_id=None, **kwargs):
            """模拟 Agent 完成前目标图片被删除。"""
            del progress, kwargs
            assert task_id
            path.unlink()
            return {}, "deleted-session"

        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_run)
        response = test_client.post("/images/context", json={"meme_id": uploaded["meme_id"]})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = test_client.get(f"/tasks/{task_id}").json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert status["status"] == "failed"
        assert status["error"]["error"] == "target_changed"
    _clear_test_scope()
