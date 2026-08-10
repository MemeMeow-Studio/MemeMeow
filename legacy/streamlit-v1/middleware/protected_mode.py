from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class ProtectedModeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config):
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next):

        # 检查保护模式状态
        if self.config.protected_mode:
            # 标准化路径：移除末尾斜杠
            path = request.url.path.rstrip('/')
            allowed_paths = [p.rstrip('/') for p in self.config.allowed_endpoints]
            
            # 放行白名单端点
            if path in allowed_paths:
                return await call_next(request)
                
            # 拦截其他请求
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"Protected mode blocked request to {path}"
                }
            )
            
        return await call_next(request)