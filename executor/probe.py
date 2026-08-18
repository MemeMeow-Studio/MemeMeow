"""Agent executor 容器内的无泄密健康探针。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from executor.token import ExecutorTokenError, read_token_file


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """健康探针禁止把 executor token 跟随跳转发送到其它地址。"""

    def redirect_request(self, *_args: object, **_kwargs: object):
        """拒绝所有重定向。"""
        return None


def main() -> int:
    """读取共享 token 并请求本地健康接口，返回 Compose 探针退出码。

    探针只输出错误码对应的退出状态，不打印 token 或响应中的敏感字段；调用
    场景是 Agent 容器 healthcheck 和运维脚本的运行时验证。
    """
    token_path = os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", "")
    try:
        token = read_token_file(Path(token_path)) if token_path else os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "")
        if not token:
            return 1
        request = urllib.request.Request(
            "http://127.0.0.1:8277/health",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)
        with opener.open(request, timeout=5) as response:
            payload = json.load(response)
        return 0 if payload.get("ready") is True and payload.get("docker_socket_absent") is True else 1
    except (ExecutorTokenError, OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
