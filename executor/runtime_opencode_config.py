"""Agent 镜像使用的无密钥 OpenCode provider 配置快照。

该文件与服务端 ``backend.opencode`` 保持同一协议结构，但不包含部署地址、
模型长期凭据或账户信息；运行时只由 broker endpoint 和短期 capability 展开。
"""

from __future__ import annotations

from typing import Any


RUNTIME_OPENCODE_CONFIG: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "experimental": {"continue_loop_on_deny": True},
    "provider": {
        "mememeow": {
            "npm": "@ai-sdk/openai",
            "name": "MemeMeow OpenAI Responses provider",
            "options": {
                "baseURL": "{env:MEMEMEOW_MODEL_BROKER_URL}",
                "apiKey": "{env:MEMEMEOW_MODEL_CAPABILITY}",
            },
            "models": {
                "gpt-5.6-luna": {
                    "id": "gpt-5.6-luna",
                    "name": "GPT-5.6 Luna",
                    "family": "gpt-luna",
                    "status": "active",
                    "reasoning": True,
                    "tool_call": True,
                    "temperature": False,
                    "attachment": True,
                    "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                    "limit": {"context": 1050000, "input": 922000, "output": 128000},
                    "variants": {"max": {"reasoningEffort": "max"}},
                }
            },
        }
    },
}

__all__ = ["RUNTIME_OPENCODE_CONFIG"]
