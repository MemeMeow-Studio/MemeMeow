"""进程内限流器的窗口计数和过期客户端清理测试。"""

from backend.rate_limiter import RateLimiter


def test_rate_limiter_rejects_excess_requests(monkeypatch):
    """同一客户端达到窗口上限后拒绝后续请求。"""
    monkeypatch.setattr("backend.rate_limiter.time.time", lambda: 100.0)
    limiter = RateLimiter()

    assert limiter.check("client", max_requests=1, window=10) is True
    assert limiter.check("client", max_requests=1, window=10) is False


def test_rate_limiter_removes_expired_clients(monkeypatch):
    """跨过清理周期后移除不再请求的客户端标识。"""
    current = 100.0
    monkeypatch.setattr("backend.rate_limiter.time.time", lambda: current)
    limiter = RateLimiter()
    assert limiter.check("expired", max_requests=1, window=10) is True

    current = 111.0
    assert limiter.check("active", max_requests=1, window=10) is True
    assert "expired" not in limiter._counts
