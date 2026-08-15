#!/usr/bin/env python3
"""research-meme-context 的 task-scoped 本地视觉匹配薄 CLI。

脚本只读取 Runner 注入的任务标识和内部 URL，输出后端已过滤的 JSON；它不连接
数据库、不读取模型权重、不接受 scope 或任意图片 ID，也不触发即时推理。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def _error(code: str, message: str, *, status: int | None = None) -> int:
    """将稳定错误写到 stderr 并返回非零退出码。"""
    payload: dict[str, object] = {"error": code, "message": message}
    if status is not None:
        payload["status"] = status
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


def _url() -> str | None:
    """读取内部地址别名，拒绝从命令行传入任意 scope 端点。"""
    return os.getenv("MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL") or os.getenv("MEMEMEOW_VISUAL_MATCH_INTERNAL_URL")


def main(argv: list[str] | None = None) -> int:
    """解析 top_k，使用当前 Runner task_id 请求后端视觉匹配。"""
    parser = argparse.ArgumentParser(description="查询当前 Agent 任务 scope 内的已研究视觉近邻")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--include-self", action="store_true")
    args = parser.parse_args(argv)
    task_id = os.getenv("MEMEMEOW_AGENT_TASK_ID")
    url = _url()
    if not task_id:
        return _error("agent_task_id_missing", "运行时未注入 MEMEMEOW_AGENT_TASK_ID")
    if not url:
        return _error("visual_search_url_missing", "运行时未注入视觉匹配内部地址")
    if args.top_k < 1 or args.top_k > 50:
        return _error("invalid_top_k", "top_k 必须在 1 至 50 之间")
    request_payload = json.dumps({"task_id": task_id, "top_k": args.top_k, "exclude_self": not args.include_self}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=request_payload, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            payload: Any = json.loads(raw)
            if response.status >= 400:
                code = payload.get("error") if isinstance(payload, dict) else "visual_search_http_error"
                return _error(str(code), str(payload.get("message") or "视觉匹配请求失败") if isinstance(payload, dict) else "视觉匹配请求失败", status=response.status)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError):
            payload = {}
        code = payload.get("error") if isinstance(payload, dict) else "visual_search_http_error"
        message = payload.get("message") if isinstance(payload, dict) else None
        return _error(str(code), str(message or "视觉匹配请求失败"), status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return _error("visual_search_unavailable", "视觉匹配内部服务暂时不可用")
    if not isinstance(payload, dict):
        return _error("visual_search_invalid_response", "视觉匹配服务返回格式无效")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
