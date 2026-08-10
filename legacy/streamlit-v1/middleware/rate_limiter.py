import time
from collections import defaultdict
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from threading import Lock
import redis
from redis.exceptions import RedisError

class RateLimiter:
    def __init__(self):
        self.counts = defaultdict(list)
        self.lock = Lock()

    def check(self, key: str, max_requests: int, window: int) -> bool:
        with self.lock:
            now = time.time()
            # 清理过期记录并删除空键
            if key in self.counts:
                self.counts[key] = [t for t in self.counts[key] if (now - t) <= window]
                if not self.counts[key]:
                    del self.counts[key]
            # 检查计数
            current = self.counts.get(key, [])
            if len(current) >= max_requests:
                return False
            self.counts[key].append(now)
            return True
        
class RedisRateLimiter:
    def __init__(self, host='localhost', port=6379):
        self.redis = redis.Redis(host=host, port=port)
        self.script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        local max = tonumber(ARGV[3])
        
        -- 删除窗口之前的记录
        redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
        -- 获取当前计数
        local count = redis.call('ZCARD', key)
        if count >= max then
            return 0
        end
        -- 添加当前时间戳
        redis.call('ZADD', key, now, now)
        -- 设置过期时间
        redis.call('EXPIRE', key, window)
        return 1
        """

    def check(self, key: str, max_requests: int, window: int) -> bool:
        try:
            now = int(time.time())
            result = self.redis.eval(
                self.script,
                1,
                key,
                now,
                window,
                max_requests
            )
            return bool(result)
        except RedisError:
            return True  # 根据需求调整降级策略

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config):
        super().__init__(app)
        self.config = config
        if self.config.rate_limit.storage == 'redis':
            self.ratelimiter = RedisRateLimiter()
        else:
            self.ratelimiter = RateLimiter()

    async def dispatch(self, request: Request, call_next):
        if not self.config.rate_limit.enabled:
            return await call_next(request)

        # 获取客户端标识
        client_ip = request.client.host
        
        # 检查速率限制
        if not self.ratelimiter.check(
            key=client_ip,
            max_requests=self.config.rate_limit.requests,
            window=self.config.rate_limit.window
        ):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": f"Requests are limited to {self.config.rate_limit.requests} per {self.config.rate_limit.window} seconds",
                },
                headers={"Retry-After": str(self.config.rate_limit.window)}
            )
            
        return await call_next(request)
