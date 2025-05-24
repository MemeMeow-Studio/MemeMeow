import os
import sys
import time

import requests
import openai
from openai import OpenAI
import pickle
from config.settings import Config
from typing import List, Optional, Union
import numpy as np
from FlagEmbedding import BGEM3FlagModel

from tqdm import tqdm
from services.utils import verify_folder
import threading


class EmbeddingService:
    def __init__(self):
        self.api_key = Config().api.embedding_models.api_key
        self.base_url = Config().api.embedding_models.base_url
        self.selected_embedding_model = Config().models.selected_embedding_model
        self.embedding_cache = {}
        self._get_embedding_cache()
        self.cache_lock = threading.Lock()
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.rpm_monitor = [0]

    def refresh_config(self):
        self.api_key = Config().api.embedding_models.api_key
        self.base_url = Config().api.embedding_models.base_url
        self.selected_embedding_model = Config().models.selected_embedding_model
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.rpm_monitor = [0]

    def _get_embedding_cache(self):
        """获取嵌入缓存"""

        cache_file = Config().get_abs_api_cache_file()
        verify_folder(cache_file)

        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as f:
                self.embedding_cache = pickle.load(f)

    def is_rpm_overload(self):
        """检查RPM是否过载"""
        pt = time.time()
        send_count = 0
        for k in self.rpm_monitor:
            if pt - k > 60:
                continue
            send_count += 1
        if send_count >= 1800:
            return True
        return False

    def get_last_request_time(self):
        """获取最后一次请求的时间"""
        return self.rpm_monitor[-1]

    def save_embedding_cache(self):
        """保存嵌入缓存"""

        cache_file = Config().get_abs_api_cache_file()

        if sys.gettrace() is not None:
            print(f'saving cache: {sum(len(i) for i in self.embedding_cache.values())}')
        with open(cache_file, 'wb') as f:
            pickle.dump(self.embedding_cache, f)

    @staticmethod
    def normalize_embedding(embedding: Union[List[float], np.ndarray]) -> np.ndarray:
        """归一化嵌入向量"""
        if isinstance(embedding, list):
            embedding = np.array(embedding)
        return embedding / np.linalg.norm(embedding)

    def get_embedding(self, text: str, key: str = None) -> np.ndarray:
        """获取文本嵌入并归一化"""
        # API 模式
        model_name = Config().models.embedding_models['bge-m3'].name
        payload = {
            "input": text,
            "model": model_name,
            "encoding_format": "float"  # 指定返回格式
        }

        self.cache_lock.acquire()
        if model_name in self.embedding_cache.keys() and text in self.embedding_cache[model_name].keys():
            if sys.gettrace() is not None:
                print(f'using cache: {model_name} {text}')
            embedding = self.embedding_cache[model_name][text]
            self.cache_lock.release()
        else:
            # 检查是否指定新的api key，如果指定则更新api key
            if key is not None and key != self.api_key:
                self.api_key = key
            self.cache_lock.release()
            try:
                response = self.client.embeddings.create(**payload)
                embedding = response.data[0].embedding
            except openai.OpenAIError as e:
                raise RuntimeError(f"API请求失败: {str(e)}\n请求参数: {payload}")
            self.cache_lock.acquire()
            if model_name not in self.embedding_cache.keys():
                self.embedding_cache[model_name] = {}
            self.embedding_cache[model_name][text] = embedding
            self.rpm_monitor.append(time.time())
            self.cache_lock.release()

        # 确保返回新的归一化向量
        return self.normalize_embedding(embedding.copy() if isinstance(embedding, np.ndarray) else embedding)