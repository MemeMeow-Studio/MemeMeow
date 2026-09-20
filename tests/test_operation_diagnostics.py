# 使用真实 Loguru、异步任务、线程和文件锁验证公共业务诊断。

import asyncio
import json
import time
from uuid import uuid4

import pytest
from loguru import logger
from starlette.concurrency import run_in_threadpool

from backend.operation_diagnostics import OperationDiagnostics
from backend.reverse_image import ReverseImageCache, ReverseImageError, ReverseImageRequest


@pytest.fixture
def events():
    """采集真实 Loguru 输出消息，结束时移除本测试安装的输出端。"""
    messages = []
    sink = logger.add(messages.append, format="{message}", filter=lambda record: "operation_fields" in record["extra"])
    try:
        yield messages
    finally:
        logger.remove(sink)


def _fields(message):
    """读取事件消息中的 JSON 字段，与部署环境的 message 输出方式一致。"""
    return json.loads(str(message).split(" ", 1)[1])


def test_timings_accumulate_and_cover_duration(events):
    """真实计时覆盖重复阶段，总时长等于各互斥阶段之和。"""
    with OperationDiagnostics("timing_test", phase="validation") as diagnostics:
        time.sleep(0.003)
        diagnostics.phase("persist")
        time.sleep(0.003)
        diagnostics.phase("validation")
        time.sleep(0.003)
    fields = _fields(events[0])
    assert fields["validation_ms"] >= 6
    assert fields["persist_ms"] >= 3
    assert fields["duration_ms"] == pytest.approx(fields["validation_ms"] + fields["persist_ms"], abs=0.002)
    assert fields["outcome"] == "success"
    assert len(events) == 1


def test_failure_preserves_original_and_final_codes_without_payload(events):
    """请求校验错误经业务错误转换后保留原因类别，消息不包含请求内容。"""
    request = ReverseImageRequest(image=b"private-payload", filename="private.txt", task_id="private-task")
    with pytest.raises(ReverseImageError) as raised:
        with OperationDiagnostics("failure_test", phase="validation") as diagnostics:
            try:
                request.normalized()
            except ReverseImageError as exc:
                diagnostics.failure(exc)
                diagnostics.phase("persist")
                raise ReverseImageError("reverse_image_unknown_execution", "private-message") from exc
    assert raised.value.code == "reverse_image_unknown_execution"
    fields = _fields(events[0])
    assert fields["error_stage"] == "validation"
    assert fields["error_code"] == "invalid_image_format"
    assert fields["final_error_stage"] == "persist"
    assert fields["final_error_code"] == "reverse_image_unknown_execution"
    assert fields["outcome"] == "unknown_execution"
    assert "private" not in str(events[0])


def test_async_and_thread_context_isolation(events):
    """并发异步任务及真实线程继承各自关联信息，退出上下文后不残留标识。"""
    identifiers = [uuid4().hex, uuid4().hex]

    def threaded():
        """在线程内输出事件，验证线程池的上下文传递。"""
        with OperationDiagnostics("thread_test", phase="provider"):
            time.sleep(0.003)

    async def request(trace_id):
        """给单次调用建立独立上下文并执行子任务与线程。"""
        with logger.contextualize(trace_id=trace_id, task_digest="digest:1234567890abcdef", token="private-token"):
            with OperationDiagnostics("async_test", phase="provider"):
                await asyncio.create_task(run_in_threadpool(threaded))

    async def exercise():
        """并发运行两个请求，检验跨 await 的隔离。"""
        await asyncio.gather(*(request(identifier) for identifier in identifiers))
        with OperationDiagnostics("after_test", phase="validation"):
            pass

    asyncio.run(exercise())
    fields = [_fields(message) for message in events]
    for identifier in identifiers:
        associated = [item for item in fields if item.get("trace_id") == identifier]
        assert len(associated) == 2
        assert all(item["task_digest"] == "digest:1234567890abcdef" for item in associated)
    assert "trace_id" not in fields[-1]
    assert all("private-token" not in str(message) for message in events)


def test_real_cache_lock_wait_and_cancellation(events, tmp_path):
    """竞争真实文件锁，确认等待耗时及取消异常均被保留。"""
    cache = ReverseImageCache(tmp_path / "cache")
    key = "a" * 64

    async def waiter():
        """在已持有的文件锁上等待，取消后输出一次终态。"""
        with OperationDiagnostics("lock_test", phase="lock_wait"):
            async with cache.lock_async(key, retry_seconds=0.001):
                pytest.fail("持有者释放之前不应获得文件锁")

    async def exercise():
        """保持文件锁并取消等待者，随后确认锁仍能正常释放与获取。"""
        async with cache.lock_async(key):
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with cache.lock_async(key):
            pass

    asyncio.run(exercise())
    fields = _fields(events[0])
    assert fields["lock_wait_ms"] >= 10
    assert fields["outcome"] == "cancelled"
    assert fields["cancel_requested"] is True
    assert len(events) == 1


def test_cancel_wait_includes_finalization(events):
    """已收到取消的操作继续保存结果时，累计等待时间包含保存阶段。"""
    async def exercise():
        """运行真实异步取消及清理路径。"""
        with OperationDiagnostics("cancel_test", phase="provider") as diagnostics:
            task = asyncio.current_task()
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                diagnostics.cancelled()
                diagnostics.phase("persist")
                await asyncio.sleep(0.01)
                raise

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())
    fields = _fields(events[0])
    assert fields["cancel_wait_ms"] >= 10
    assert fields["persist_ms"] >= 10
    assert fields["outcome"] == "cancelled"
