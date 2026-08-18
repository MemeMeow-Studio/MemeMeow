"""进程内长任务状态机测试。"""

from __future__ import annotations

import time
from threading import Event, Lock

from backend.tasks import PersistentTaskService, TaskManager


def wait_for_terminal(manager: TaskManager, task_id: str, timeout: float = 2.0):
    """轮询测试任务直到终态，超时即失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(task_id)
        if record and record.status in {"succeeded", "failed"}:
            return record
        time.sleep(0.01)
    raise AssertionError("任务未在测试时限内完成")


def test_task_succeeds_and_reports_progress():
    """成功任务按 queued/running/succeeded 状态机结束。"""
    manager = TaskManager(max_workers=1)
    record = manager.submit("cache_generation", lambda progress: progress(0.5, "half"))
    completed = wait_for_terminal(manager, record.task_id)
    assert completed.status == "succeeded"
    assert completed.progress == 1.0
    assert completed.completed_at is not None
    manager.shutdown()


def test_duplicate_active_type_returns_same_task():
    """同类型未完成任务不会并发执行。"""
    release = Event()
    manager = TaskManager(max_workers=1)
    first = manager.submit("cache_generation", lambda progress: release.wait(1))
    second = manager.submit("cache_generation", lambda progress: None)
    assert second.task_id == first.task_id
    release.set()
    wait_for_terminal(manager, first.task_id)
    manager.shutdown()


def test_failure_is_diagnostic_and_shutdown_marks_pending_failed():
    """异常和服务关闭都生成稳定失败信息。"""
    manager = TaskManager(max_workers=1)

    def fail(progress):
        raise RuntimeError("boom")

    failed = manager.submit("failure", fail)
    completed = wait_for_terminal(manager, failed.task_id)
    assert completed.status == "failed"
    assert completed.error["error"] == "task_failed"

    release = Event()
    pending = manager.submit("pending", lambda progress: release.wait(1))
    manager.shutdown()
    stopped = manager.get(pending.task_id)
    assert stopped.status == "failed"
    assert stopped.error["error"] == "task_not_recoverable"
    release.set()


def test_agent_lane_runs_different_images_in_parallel_and_keeps_cache_lane_available(tmp_path):
    """Agent lane 受并发上限控制，cache 任务仍可在 Agent 满载时启动。"""
    manager = PersistentTaskService(tmp_path / "tasks", max_workers=1, agent_concurrency=2)
    active = 0
    maximum = 0
    lock = Lock()
    release = Event()

    def context(payload, progress):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(1)
        with lock:
            active -= 1

    cache_started = Event()

    def cache(payload, progress):
        cache_started.set()

    manager.register("meme_context_generation", context)
    manager.register("cache_generation", cache)
    manager.start()
    first = manager.submit("meme_context_generation", {"image_relative_path": "a.png"})
    second = manager.submit("meme_context_generation", {"image_relative_path": "b.png"})
    queued = manager.submit("meme_context_generation", {"image_relative_path": "c.png"})
    manager.submit("cache_generation", {})
    assert queued.message == "Agent lane 背压排队"
    assert cache_started.wait(1)
    release.set()
    assert wait_for_terminal(manager, first.task_id).status == "succeeded"
    assert wait_for_terminal(manager, second.task_id).status == "succeeded"
    assert wait_for_terminal(manager, queued.task_id).status == "succeeded"
    assert maximum == 2
    manager.shutdown()


def test_batch_finalizer_submits_one_deduplicated_cache_task(tmp_path):
    """批次最后一项终态后只触发一次缓存回调。"""
    manager = PersistentTaskService(tmp_path / "tasks", agent_concurrency=2)
    finalized = []
    manager.set_batch_finalizer(finalized.append)
    manager.register("meme_context_generation", lambda payload, progress: None)
    manager.start()
    first = manager.submit("meme_context_generation", {"image_relative_path": "a.png", "batch_id": "batch"})
    second = manager.submit("meme_context_generation", {"image_relative_path": "b.png", "batch_id": "batch"})
    assert wait_for_terminal(manager, first.task_id).status == "succeeded"
    assert wait_for_terminal(manager, second.task_id).status == "succeeded"
    for _ in range(50):
        if finalized:
            break
        time.sleep(0.01)
    assert finalized == ["batch"]
    manager.shutdown()


def test_context_dedupe_uses_image_identity_not_transient_batch_or_auto_name(tmp_path):
    """同一图片即使来自不同批次或自动命名选项也只保留一个活动任务。"""
    manager = PersistentTaskService(tmp_path / "tasks")
    release = Event()
    manager.register("meme_context_generation", lambda payload, progress: release.wait(1))
    manager.start()
    first = manager.submit("meme_context_generation", {"image_relative_path": "a.png", "image_sha256": "a" * 64, "auto_name": False, "batch_id": "one"})
    second = manager.submit("meme_context_generation", {"image_relative_path": "a.png", "image_sha256": "a" * 64, "auto_name": True, "batch_id": "two"})
    assert second.task_id == first.task_id
    release.set()
    wait_for_terminal(manager, first.task_id)
    manager.shutdown()


def test_reused_context_task_finalizes_every_associated_batch(tmp_path):
    """活动任务跨批次复用时，每个批次仍只触发一次缓存合并。"""
    manager = PersistentTaskService(tmp_path / "tasks")
    finalized = []
    release = Event()
    manager.set_batch_finalizer(finalized.append)
    manager.register("meme_context_generation", lambda payload, progress: release.wait(1))
    manager.start()
    first = manager.submit("meme_context_generation", {"image_relative_path": "a.png", "image_sha256": "a" * 64, "batch_id": "one"})
    second = manager.submit("meme_context_generation", {"image_relative_path": "a.png", "image_sha256": "a" * 64, "batch_id": "two"})
    assert second.task_id == first.task_id
    release.set()
    assert wait_for_terminal(manager, first.task_id).status == "succeeded"
    for _ in range(50):
        if sorted(finalized) == ["one", "two"]:
            break
        time.sleep(0.01)
    assert sorted(finalized) == ["one", "two"]
    manager.shutdown()


def test_standalone_image_stage_dedupe_ignores_submission_nonce(tmp_path):
    """独立图片阶段的并发 nonce 不应绕过去重并重复执行同一输入。"""
    manager = PersistentTaskService(tmp_path / "tasks")
    release = Event()
    manager.register("image_auto_rename", lambda payload, progress: release.wait(1))
    manager.start()
    payload = {
        "submission_mode": "standalone",
        "stage": "auto_rename",
        "meme_id": "meme-1",
        "image_sha256": "a" * 64,
        "expected_storage_key": "source.png",
        "title_fingerprint": "b" * 64,
    }
    first = manager.submit("image_auto_rename", {**payload, "standalone_submission_nonce": "one"})
    second = manager.submit("image_auto_rename", {**payload, "standalone_submission_nonce": "two"})
    assert second.task_id == first.task_id
    release.set()
    assert wait_for_terminal(manager, first.task_id).status == "succeeded"
    manager.shutdown()
