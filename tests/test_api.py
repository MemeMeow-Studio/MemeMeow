"""FastAPI 公共契约、文件边界和上传行为测试。"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from api import app
from backend.database import create_engine_for_url


def _clear_test_scope() -> None:
    """清理 API 测试使用的 local scope 业务行，保留安装标记和 scope 命名空间。"""
    url = os.getenv("MEMEMEOW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，拒绝清理开发数据库")
    engine = create_engine_for_url(url, pool_size=1, max_overflow=0)
    statements = (
        "DELETE FROM task_lane_slots",
        "DELETE FROM task_batch_items",
        "DELETE FROM task_batches",
        "DELETE FROM tasks",
        "DELETE FROM meme_visual_embeddings",
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
    _clear_test_scope()
    with TestClient(app) as test_client:
        uploaded = test_client.post("/images/upload", files=[("files", ("deleted.png", png_bytes(), "image/png"))]).json()["results"][0]

        def fake_run(path, progress, *, task_id=None):
            """模拟 Agent 完成前目标图片被删除。"""
            assert task_id
            path.unlink()
            return {}, "deleted-session"

        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_run)
        response = test_client.post("/images/context", json={"meme_id": uploaded["meme_id"]})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        for _ in range(100):
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


def test_parallel_context_batch_writes_independent_database_records_and_merges_cache(tmp_path, monkeypatch):
    """并发度为 2 时两张图片使用独立任务，批次终态只提交一次缓存任务。"""
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "2")
    _clear_test_scope()
    with TestClient(app) as test_client:
        first = test_client.post("/images/upload", files=[("files", ("parallel-a.png", png_bytes("red"), "image/png"))]).json()["results"][0]
        second = test_client.post("/images/upload", files=[("files", ("parallel-b.png", png_bytes("blue"), "image/png"))]).json()["results"][0]
        started = threading.Barrier(2)
        sessions: list[str] = []

        def fake_run(image, progress, *, task_id=None):
            """模拟两个独立 Agent session，并在 barrier 处确认真正并行。"""
            assert task_id
            sessions.append(image.name)
            started.wait(timeout=2)
            return {
                "title": image.stem,
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
        deadline = time.monotonic() + 2
        while len(sessions) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(sessions) == {"parallel-a.png", "parallel-b.png"}
        for task_id in task_ids:
            for _ in range(100):
                record = test_client.get(f"/tasks/{task_id}").json()
                if record["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
        assert record["status"] == "succeeded"
        assert test_client.get(f"/images/metadata?meme_id={first['meme_id']}").json()["context_status"] == "ready"
        assert test_client.get(f"/images/metadata?meme_id={second['meme_id']}").json()["context_status"] == "ready"
        for _ in range(100):
            cache_items = test_client.get("/tasks", params={"task_type": "cache_generation"}).json()["items"]
            if cache_items:
                break
            time.sleep(0.01)
        assert len(cache_items) == 1
    _clear_test_scope()


def test_context_target_deleted_during_agent_is_reported_as_target_changed(tmp_path, monkeypatch):
    """Agent 运行期间图片被删除时，任务必须返回稳定的目标变化错误。"""
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "")
    _clear_test_scope()
    with TestClient(app) as test_client:
        uploaded = test_client.post("/images/upload", files=[("files", ("deleted.png", png_bytes(), "image/png"))]).json()["results"][0]

        def fake_run(path, progress, *, task_id=None):
            """模拟 Agent 完成前目标图片被删除。"""
            assert task_id
            path.unlink()
            return {}, "deleted-session"

        monkeypatch.setattr(test_client.app.state.opencode, "run", fake_run)
        response = test_client.post("/images/context", json={"meme_id": uploaded["meme_id"]})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        for _ in range(100):
            status = test_client.get(f"/tasks/{task_id}").json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert status["status"] == "failed"
        assert status["error"]["error"] == "target_changed"
    _clear_test_scope()
