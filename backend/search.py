"""不依赖资源包的本地图片语义检索服务。

缓存使用显式版本的 JSON 格式，并通过原子替换刷新；历史 pickle 资源包缓存不会被加载。
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from threading import Lock
from typing import Callable

import numpy as np
from openai import OpenAI

from backend.config import Settings
from backend.metadata import MetadataError, MetadataService
from backend.paths import SUPPORTED_EXTENSIONS


CACHE_VERSION = 4


class SearchService:
    """管理本地图片索引、缓存生成和稳定语义检索。"""

    def __init__(self, settings: Settings, metadata: MetadataService | None = None):
        self.settings = settings
        self.metadata = metadata or MetadataService(settings.image_root)
        self.cache_path = settings.data_root / "search-cache-v4.json"
        self._lock = Lock()
        self._items: list[dict[str, object]] | None = self._load_cache()

    def _client(self) -> OpenAI:
        """按需创建嵌入客户端，缺少配置时只影响相关操作。"""
        if not self.settings.embedding_api_key or not self.settings.embedding_base_url:
            raise RuntimeError("embedding_not_configured")
        return OpenAI(api_key=self.settings.embedding_api_key, base_url=self.settings.embedding_base_url)

    def _load_cache(self) -> list[dict[str, object]] | None:
        """读取当前版本缓存；格式不匹配时明确拒绝。"""
        if not self.cache_path.is_file():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if payload.get("version") != CACHE_VERSION or payload.get("model") != self.settings.embedding_model:
                return None
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                return None
            records: dict[str, dict[str, object]] = {}
            for path in self.settings.image_root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                relative = path.resolve().relative_to(self.settings.image_root.resolve()).as_posix()
                record = self.metadata.embedding_record(path)
                if record["indexable"]:
                    records[relative] = record
            if {str(item.get("path")) for item in items if isinstance(item, dict)} != set(records):
                return None
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                    return None
                image = self.settings.image_root / str(item.get("path"))
                try:
                    image.resolve().relative_to(self.settings.image_root.resolve())
                except ValueError:
                    return None
                current = records.get(str(item.get("path")))
                if not current or item.get("image_sha256") != current["image_sha256"] or item.get("metadata_hash") != current["metadata_hash"]:
                    return None
                text = str(current["text"])
                if item.get("semantic_document_hash") != hashlib.sha256(text.encode("utf-8")).hexdigest():
                    return None
            return items
        except (MetadataError, OSError, ValueError, TypeError):
            return None

    def has_cache(self) -> bool:
        """判断是否存在已完整加载的当前版本缓存。"""
        with self._lock:
            return self._items is not None

    def invalidate_cache(self) -> None:
        """使当前进程不再使用受元数据变更影响的旧索引。"""
        with self._lock:
            self._items = None

    def mark_cache_invalidated(self, batch_id: object = None) -> None:
        """记录图片级缓存失效；批次完成前不触发全库 embedding 重建。"""
        self.invalidate_cache()

    def _embedding(self, text: str) -> list[float]:
        """调用模型生成归一化向量。"""
        response = self._client().embeddings.create(model=self.settings.embedding_model, input=text, encoding_format="float")
        vector = np.asarray(response.data[0].embedding, dtype=float)
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise RuntimeError("embedding_invalid")
        return (vector / norm).tolist()

    def generate_cache(self, progress: Callable[[float | None, str | None], None]) -> dict[str, object]:
        """扫描图片并生成新缓存，成功后才替换仍可用的旧缓存。"""
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        paths = sorted(
            (path for path in self.settings.image_root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda path: path.relative_to(self.settings.image_root).as_posix(),
        )
        if not paths:
            raise RuntimeError("image_library_empty")
        items: list[dict[str, object]] = []
        skipped: dict[str, int] = {}
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            relative = path.resolve().relative_to(self.settings.image_root.resolve()).as_posix()
            record = self.metadata.embedding_record(path)
            if not record["indexable"]:
                reason = str(record.get("skip_reason") or "metadata_unavailable")
                skipped[reason] = skipped.get(reason, 0) + 1
                progress(index / total, f"正在检查 {index}/{total}")
                continue
            text = str(record["text"])
            items.append(
                {
                    "path": relative,
                    "label": text,
                    "semantic_document": text,
                    "semantic_document_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "metadata_schema_version": record["metadata_schema_version"],
                    "metadata_hash": record["metadata_hash"],
                    "image_sha256": record["image_sha256"],
                    "metadata_status": record["status"],
                    "embedding": self._embedding(text),
                }
            )
            progress(index / total, f"正在处理 {index}/{total}")
        if not items:
            raise RuntimeError("no_indexable_images")
        payload = {
            "version": CACHE_VERSION,
            "model": self.settings.embedding_model,
            "indexed_count": len(items),
            "skipped_count": sum(skipped.values()),
            "skipped_by_reason": skipped,
            "items": items,
        }
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.cache_path)
        with self._lock:
            self._items = items
        return {"indexed_count": len(items), "skipped_count": sum(skipped.values()), "skipped_by_reason": skipped}

    def _enhance_query(self, query: str) -> str:
        """使用可选聊天模型改写查询；任何异常由调用方回退。"""
        if not self.settings.llm_enhance_model:
            raise RuntimeError("llm_enhance_not_configured")
        response = self._client().chat.completions.create(
            model=self.settings.llm_enhance_model,
            messages=[{"role": "user", "content": f"将下面内容改写为一句适合检索表情包的简短描述，只输出描述：{query}"}],
            temperature=0.2,
        )
        value = response.choices[0].message.content
        if not value or not value.strip():
            raise RuntimeError("llm_enhance_invalid")
        return value.strip()

    def search(self, query: str, top_k: int = 5, api_key: str | None = None, use_llm: bool = False) -> list[str]:
        """按相关性和稳定路径次级键返回可访问且去重的图片路径。"""
        with self._lock:
            items = list(self._items or [])
        if use_llm:
            try:
                query = self._enhance_query(query)
            except Exception:  # noqa: BLE001
                pass
        query_embedding = np.asarray(self._embedding(query), dtype=float)
        ranked: list[tuple[float, str]] = []
        for item in items:
            relative = str(item["path"])
            path = (self.settings.image_root / relative).resolve()
            try:
                path.relative_to(self.settings.image_root.resolve())
            except ValueError:
                continue
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            vector = np.asarray(item["embedding"], dtype=float)
            if vector.shape != query_embedding.shape:
                continue
            ranked.append((float(np.dot(query_embedding, vector)), relative))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        results: list[str] = []
        for _, relative in ranked:
            absolute = str((self.settings.image_root / relative).resolve())
            if absolute not in results:
                results.append(absolute)
            if len(results) >= top_k:
                break
        return results
