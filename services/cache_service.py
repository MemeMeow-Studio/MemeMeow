import os
import threading
import time

import numpy as np
import pickle
import re
from typing import Optional, List, Dict

from config.settings import Config
from stpages.utils import ENDWITH_IMAGE

from services.embedding_service import EmbeddingService
from services.resource_pack_manager import ResourcePackManager
from services.utils import *
from services.llm_enhance import LLMEnhance


class CacheService:

    def __init__(self, emb_srv: EmbeddingService, rp_mgr: ResourcePackManager):
        self.embedding_service = emb_srv
        self.resource_pack_manager = rp_mgr

    def generate_cache(self, progress_bar) -> None:
        # 获取所有启用的资源包
        enabled_packs = self.resource_pack_manager.get_enabled_packs()
        if not enabled_packs:
            raise RuntimeError("没有启用的资源包")

        # 为每个资源包生成缓存
        total_packs = len(enabled_packs)
        success_count = 0
        failed_packs = []

        for i, (pack_id, pack_info) in enumerate(enabled_packs.items()):
            progress_bar.progress(i / total_packs, text=f"处理资源包 {i + 1}/{total_packs}: {pack_info['name']}")
            try:
                self._generate_pack_cache(pack_id, pack_info, progress_bar)
                success_count += 1
            except Exception as e:
                logger.error(f"生成资源包 {pack_info['name']} 的缓存失败: {e}")
                failed_packs.append(f"{pack_info['name']}: {str(e)}")

        # 如果有失败的资源包，报告错误
        if failed_packs:
            error_message = f"成功生成 {success_count}/{total_packs} 个资源包的缓存。\n以下资源包生成失败:\n" + "\n".join(
                failed_packs)
            raise RuntimeError(error_message)

    def try_load_cache(self) -> t.List | None:

        """
        尝试加载缓存
        重新写入self.image_data
        """
        # 获取所有启用的资源包的缓存文件
        cache_files = self.resource_pack_manager.get_cache_files()

        if not cache_files:
            return None

        # 合并所有缓存文件的数据
        all_embeddings = []

        for pack_id, cache_file in cache_files.items():
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'rb') as f:
                        cached_data = pickle.load(f)
                    valid_embeddings = []
                    for item in cached_data:
                        # 获取文件路径
                        if 'pack_id' not in item:
                            item['pack_id'] = pack_id
                        valid_embeddings.append(item)

                    if valid_embeddings:
                        all_embeddings.extend(valid_embeddings)
                        # 更新缓存文件
                        if len(valid_embeddings) != len(cached_data):
                            with open(cache_file, 'wb') as f:
                                pickle.dump(valid_embeddings, f)
                except (pickle.UnpicklingError, EOFError) as e:
                    print(f"加载缓存文件 {cache_file} 失败: {str(e)}")

        if all_embeddings:
            return all_embeddings
        else:
            return None

    def _generate_pack_cache(self, pack_id: str, pack_info: Dict, progress_bar) -> None:
        self.embedding_service.refresh_config()
        """为指定的资源包生成缓存"""
        img_dir = pack_info["path"]

        cache_file = self.resource_pack_manager.get_pack_cache_file(pack_id)
        verify_folder(cache_file)

        # 尝试加载现有缓存
        existing_embeddings = []
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    loaded_data = pickle.load(f)

                # 验证加载的数据格式
                if isinstance(loaded_data, list):
                    # 过滤掉不是字典或缺少必要键的元素
                    valid_embeddings = []
                    for item in loaded_data:
                        if isinstance(item, dict) and 'filename' in item and 'embedding' in item:
                            valid_embeddings.append(item)
                        else:
                            logger.warning(f"警告: 缓存文件中发现无效的数据项: {type(item)}")
                    existing_embeddings = valid_embeddings
                else:
                    logger.warning(f"警告: 缓存文件格式不正确，期望列表但得到 {type(loaded_data)}")
            except (pickle.UnpicklingError, EOFError) as e:
                logger.error(f"加载缓存文件 {cache_file} 失败: {str(e)}")
                existing_embeddings = []

        # 确保所有缓存数据都有filepath字段
        generated_files = []
        for item in existing_embeddings:
            generated_files.append(item['filepath'])

        # 获取所有图片文件路径
        all_files = []
        for k, v in pack_info['manifest']['contents']['images']['files'].items():
            all_files.append(os.path.join(pack_info['pack_dir'], v['filepath']))

        image_files = [
            f for f in all_files
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))
        ]

        # 过滤掉已经生成过嵌入的文件
        new_image_files = [f for f in image_files if f not in generated_files]

        if not new_image_files and existing_embeddings:
            # 如果没有新文件且已有缓存，直接返回
            return

        # 获取资源包类型
        image_type = pack_info.get("type", "vv")

        # 获取替换规则
        replace_patterns_regex = None
        if "regex" in pack_info:
            replace_patterns_regex = {pack_info["regex"]["pattern"]: pack_info["regex"]["replacement"]}

        # 生成新文件的嵌入
        embeddings = existing_embeddings.copy()
        errors = []

        # 创建线程列表和线程锁
        embedding_lock = threading.Lock()

        total_files = len(new_image_files)

        def save_embeddings():
            if embeddings:
                with embedding_lock:
                    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                    with open(cache_file, 'wb') as f:
                        pickle.dump(embeddings, f)

                    self.embedding_service.cache_lock.acquire()
                    self.embedding_service.save_embedding_cache()
                    self.embedding_service.cache_lock.release()
        for index, filepath in enumerate(new_image_files):
            try:
                # if not os.path.isabs(filepath):
                #     filepath = os.path.join(Config().base_dir, filepath)

                filename = os.path.splitext(os.path.basename(filepath))[0]
                full_filename = None

                for ext in ENDWITH_IMAGE:
                    # if os.path.exists(os.path.join(os.path.dirname(filepath), filename + ext)):
                    #     full_filename = filename + ext
                    #     break
                    pass
                full_filename = filepath


                if full_filename:
                    raw_embedding_name = filename
                    if replace_patterns_regex is not None:
                        for pattern, replacement in replace_patterns_regex.items():
                            raw_embedding_name = re.sub(pattern, replacement, raw_embedding_name)

                    embedding_names = raw_embedding_name.split('-')
                    for embedding_name in embedding_names:
                        if embedding_name == '':
                            continue

                        def add_embedding_thread(embedding_service: EmbeddingService, store_embedding_list: List,
                                                 filename_: str, filepath_: str, embedding_name_: str,
                                                 image_type_: str, pack_id_: str, lock: threading.Lock,
                                                 errors_list: List):
                            try:
                                embedding = embedding_service.get_embedding(embedding_name_)
                                with lock:
                                    store_embedding_list.append({
                                        "filename": filename_,
                                        "filepath": filepath_,
                                        "embedding": embedding,
                                        "embedding_name": embedding_name_,
                                        "type": image_type_ if image_type_ is not None else 'Normal',
                                        "pack_id": pack_id_
                                    })
                            except Exception as e:
                                error_msg = f"生成嵌入失败 {str(e)} in [{filepath_}]"
                                print(error_msg)
                                with lock:
                                    errors_list.append(f"{str(e)} [{filepath_}]")



                        while self.embedding_service.is_rpm_overload():
                            print(f"RPM过载，等待1秒...")
                            time.sleep(1)

                        # 创建并启动线程
                        thread = threading.Thread(
                            target=add_embedding_thread,
                            args=(self.embedding_service, embeddings, filename, filepath,
                                  embedding_name, image_type, pack_id, embedding_lock, errors)
                        )
                        thread.start()

                progress_bar.progress((index + 1) / total_files,
                                      text=f"处理 {pack_info['name']} 图片 {index + 1}/{total_files}")

                if (index % 151 == 0 and index > 0 and
                        time.time() - self.embedding_service.get_last_request_time() < 30):
                    # 保存中间缓存
                    save_embeddings()

            except Exception as e:
                print(f"生成嵌入失败 [{filepath}]: {str(e)}")
                errors.append(f"[{filepath}] {str(e)}")

        # 保存最终缓存
        save_embeddings()

        # 提出错误
        if errors:
            error_summary = "\n".join(errors)
            print(error_summary)
            raise RuntimeError(error_summary)