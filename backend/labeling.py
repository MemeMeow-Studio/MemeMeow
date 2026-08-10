"""VLM 图片描述服务。

该模块只负责单张图片的候选描述生成，不直接改名；文件修改必须由用户确认后的 API 完成。
"""

from __future__ import annotations

import base64
from pathlib import Path

from openai import OpenAI

from backend.config import Settings


class LabelingService:
    """按需调用 VLM 并将文本响应转换为候选描述列表。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def describe(self, path: Path) -> list[str]:
        """读取图片并返回非空候选描述；未配置或调用失败时抛出稳定错误。"""
        if not self.settings.vlm_api_key or not self.settings.vlm_base_url:
            raise RuntimeError("vlm_not_configured")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        client = OpenAI(api_key=self.settings.vlm_api_key, base_url=self.settings.vlm_base_url)
        last_error: Exception | None = None
        for _ in range(self.settings.vlm_max_attempts):
            try:
                response = client.chat.completions.create(
                    model=self.settings.vlm_model,
                    messages=[
                        {"role": "system", "content": "请为图片生成三条简短中文表情包描述，每行一条，不要编号。"},
                        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]},
                    ],
                    max_tokens=256,
                    temperature=0.3,
                )
                content = response.choices[0].message.content or ""
                candidates = []
                for line in content.splitlines():
                    value = line.strip().lstrip("-•*0123456789. ").strip()
                    if value and value not in candidates:
                        candidates.append(value[:80])
                if candidates:
                    return candidates[:5]
                last_error = RuntimeError("vlm_invalid_response")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError("vlm_failed_after_retries") from last_error
