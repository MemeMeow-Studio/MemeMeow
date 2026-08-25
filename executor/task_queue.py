"""executor 的受控线程队列适配器。

该模块把 ``ThreadPoolExecutor`` 的生命周期从业务任务状态和 HTTP 入口中隔离；
调用方只提交已经完成校验的 callable，队列不接受 shell 或任意环境参数。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class ExecutionQueue:
    """固定 worker 数量的 executor 队列，提供兼容 submit/shutdown 接口。"""

    def __init__(self, max_workers: int, *, thread_name_prefix: str = "mememeow-executor") -> None:
        """创建有界 worker 池；并发值必须是正整数。"""
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("executor_concurrency_invalid")
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

    def submit(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        """提交已完成身份校验的执行 callable。"""
        return self._pool.submit(function, *args, **kwargs)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        """停止接受新任务，并按调用方选择等待/取消排队 future。"""
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)


__all__ = ["ExecutionQueue"]
