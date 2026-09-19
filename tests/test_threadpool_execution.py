import asyncio
from contextvars import ContextVar
import threading
from functools import partial

import anyio
import pytest
from starlette.concurrency import run_in_threadpool

from backend.reverse_image_http import _run_sync_search
from backend.visual import VisualEmbeddingError
from backend.reverse_image import NetworkReverseImageSearchAdapter, ReverseImageRequest
import visual_service


# 使用真实线程和同步等待，验证 HTTP callback 的线程调度与取消处理。
def test_sync_search_preserves_context() -> None:
    """验证同步 callback 在线程中仍能读取调用方的 ContextVar。"""
    async def exercise() -> None:
        request_context = ContextVar("request_context", default="missing")
        request_context.set("threadpool-test")
        assert await _run_sync_search(request_context.get, "missing") == "threadpool-test"
        assert await run_in_threadpool(threading.get_ident) != threading.get_ident()

    asyncio.run(exercise())


def test_provider_adapter_uses_worker_context() -> None:
    """通过真实 ContextVar 的同步读取验证 provider 适配器传递请求上下文。"""
    async def exercise() -> None:
        context = ContextVar("provider_context")
        context.set({"status": "ok"})
        provider = NetworkReverseImageSearchAdapter(context.get)
        request = ReverseImageRequest(image=b"", filename="image", task_id="threadpool-validation")
        assert await provider.search_async(request) == {"status": "ok"}

    asyncio.run(exercise())


def test_sync_search_cancellation_waits_for_actual_work() -> None:
    """通过真实 Event 等待验证重复取消仍等待工作完成，事件循环能够继续执行。"""
    async def exercise() -> None:
        release = threading.Event()
        limiter = anyio.to_thread.current_default_thread_limiter()
        baseline = limiter.borrowed_tokens
        task = asyncio.create_task(_run_sync_search(release.wait, 5.0))
        try:
            async with asyncio.timeout(2):
                while limiter.borrowed_tokens == baseline:
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert limiter.borrowed_tokens == baseline + 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        assert limiter.borrowed_tokens == baseline

    asyncio.run(exercise())


def test_sync_search_propagates_worker_error() -> None:
    """真实文件打开失败时，同步 callback 保留原始异常类型和 errno。"""
    async def exercise() -> None:
        with pytest.raises(FileNotFoundError) as captured:
            await _run_sync_search(open, "tests/threadpool-missing-directory/input.bin")
        assert captured.value.errno == 2

    asyncio.run(exercise())


def test_visual_decode_error_releases_capacity() -> None:
    """真实图片解码失败后释放推理名额，使后续请求能够执行并返回准确错误。"""
    async def exercise() -> None:
        for _ in range(2):
            with pytest.raises(VisualEmbeddingError) as captured:
                await visual_service._run_visual(visual_service.runner.embed, b"invalid-image")
            assert captured.value.code == "visual_image_decode_failed"
            assert not visual_service._VISUAL_CONCURRENCY.locked()

    asyncio.run(exercise())


def test_visual_cancellation_keeps_slot_until_worker_finishes() -> None:
    """使用真实 Event 验证视觉执行被取消时，线程结束之前仍占用模型名额。"""
    async def exercise() -> None:
        release = threading.Event()
        limiter = anyio.to_thread.current_default_thread_limiter()
        baseline = limiter.borrowed_tokens
        task = asyncio.create_task(visual_service._run_visual(partial(release.wait, 5.0)))
        try:
            async with asyncio.timeout(2):
                while limiter.borrowed_tokens == baseline:
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert visual_service._VISUAL_CONCURRENCY.locked()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        assert not visual_service._VISUAL_CONCURRENCY.locked()
        assert limiter.borrowed_tokens == baseline

    asyncio.run(exercise())
