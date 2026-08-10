"""FastAPI 使用的进程内请求限流器。

该模块只保留当前 API 实际使用的内存限流实现；旧版 Redis 限流和
Starlette 中间件属于已停用的 Streamlit 配置体系，不再作为运行依赖。
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class RateLimiter:
    """按客户端标识记录固定时间窗口内请求次数的线程安全限流器。"""

    def __init__(self) -> None:
        """初始化客户端请求时间戳和保护其访问的锁。"""
        self._counts: defaultdict[str, list[float]] = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = time.time()

    def check(self, key: str, max_requests: int, window: int) -> bool:
        """判断本次请求是否放行，并在放行时记录当前时间戳。

        参数 `key` 是客户端标识，`max_requests` 和 `window` 定义时间窗口。
        返回 `True` 表示允许请求，返回 `False` 表示已超过限制。
        """
        with self._lock:
            now = time.time()
            if now - self._last_cleanup >= window:
                # 周期性清理不再访问的客户端，避免来源标识持续累积。
                self._counts = defaultdict(
                    list,
                    {
                        client: [timestamp for timestamp in values if now - timestamp <= window]
                        for client, values in self._counts.items()
                        if any(now - timestamp <= window for timestamp in values)
                    },
                )
                self._last_cleanup = now
            timestamps = [timestamp for timestamp in self._counts[key] if now - timestamp <= window]
            if len(timestamps) >= max_requests:
                self._counts[key] = timestamps
                return False
            timestamps.append(now)
            self._counts[key] = timestamps
            return True
