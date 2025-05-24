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
from services.cache_service import CacheService


class ImageSearch:
    def __init__(self):
        self.embedding_service = EmbeddingService()
        self.resource_pack_manager = ResourcePackManager()
        self.cache_service = CacheService(self.embedding_service, self.resource_pack_manager)
        try:
            self.llm_enhance = LLMEnhance()
        except:
            self.llm_enhance = None
        self.image_data = None
        self._try_load_cache()

    # def __reload_class_cache(self):
    #     self.embedding_service = EmbeddingService()

    def _try_load_cache(self) -> None:
        self.embedding_service.refresh_config()
        self.image_data = CacheService(self.embedding_service, self.resource_pack_manager).try_load_cache()

    def set_mode(self, model_name) -> None:
        """切换搜索模式和模型"""
        #TODO: set mode
        # 清空当前缓存
        self.image_data = None
        # 尝试加载新模式/模型的缓存
        self._try_load_cache()

    def get_model_name(self) -> str:
        """获取当前模型名称"""
        return self.embedding_service.selected_embedding_model

    def has_cache(self) -> bool:
        """检查是否有可用的缓存"""
        return self.image_data is not None

    def generate_cache(self, progress_bar = None) -> None:
        self.embedding_service.refresh_config()
        CacheService(self.embedding_service, self.resource_pack_manager).generate_cache(progress_bar)
        # 重新加载所有缓存
        progress_bar.progress(1.0, text="重新加载缓存...")
        self._try_load_cache()
        self.embedding_service.refresh_config()

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """余弦相似度计算"""
        return np.dot(a, b)

    def search(self,
               query: str,
               top_k: int = 5,
               resource_pack_uuids: Optional[List[str]]|None = None,
               api_key: Optional[str] = None,
               use_llm: bool = False,
               return_type = 'default') -> List[str]:
        self.embedding_service.refresh_config()
        if use_llm:
            if self.llm_enhance is None:
                self.llm_enhance = LLMEnhance()
            query = self.llm_enhance.search(query)

        """语义搜索最匹配的图片"""
        if not self.has_cache():
            return []

        try:
            query_embedding = self.embedding_service.get_embedding(query, api_key)
        except Exception as e:
            print(f"查询嵌入生成失败: {str(e)}")
            return []

        similarities = []
        for img in self.image_data:

            # 排除不在resource_pack_uuids的图片。#TODO：预加载，避免每次都循环
            if resource_pack_uuids is not None:
                pack_uuid = self.resource_pack_manager.get_available_packs().get(img['pack_id']).get("uuid", None)
                if pack_uuid is not None:
                    if pack_uuid not in resource_pack_uuids:
                        continue



            if 'filepath' not in img and Config().misc.adapt_for_old_version:
                # 使用资源包的路径
                pack_id = img.get('pack_id', 'default_pack')
                pack_info = self.resource_pack_manager.get_enabled_packs().get(pack_id)
                if not pack_info:
                    continue
                    
                pack_path = pack_info["path"]
                if not os.path.isabs(pack_path):
                    pack_path = os.path.join(Config().base_dir, pack_path)

                img['filepath'] = os.path.join(pack_path, img["filename"])
                
            # if os.path.exists(img['filepath']):
                # similarity = self._cosine_similarity(query_embedding, img['embedding'])
            similarities.append(({
                                     'path': img['filepath'],
                                     'embedding_name': img['embedding_name'],
                                 "obj": img},
                                 self._cosine_similarity(query_embedding, img["embedding"])))

        if not similarities:
            return []

        exists_imgs_path = []
        # 按相似度降序排序并返回前top_k个结果
        sorted_items = sorted(similarities, key=lambda x: x[1], reverse=True)
        return_list = []
        count = 0
        download_list = []
        for i in sorted_items:
            if count >= top_k * 5:
                break
            if i[0]['path'] not in exists_imgs_path:
                if not os.path.exists(i[0]['path']):
                    # 联网检查
                    url = self.resource_pack_manager.enabled_packs[i[0]['obj']['pack_id']]['url']
                    if url:
                        rel_path = re.sub(r'^.*?resource_packs\\[^\\]+\\', '', i[0]['path'])
                        download_list.append([os.path.join(url, rel_path), i[0]['path']])
                        # if not download_file(os.path.join(url, rel_path), i[0]['path']):
                        #     continue
                    else:
                        logger.error(f"图片不存在: {i[0]['path']}")
                        continue
                return_list.append(i[0])
                exists_imgs_path.append(i[0]['path'])
                count += 1
        # 联网下载不存在的图片
        download_files(download_list)

        # 随机化输出 去除重复图片

        skip_indexes = []
        return_list_2 = []
        for index, i in enumerate(return_list):
            # 验证图片是否存在
            if not os.path.exists(i['path']):
                continue
            if len(return_list_2) >= top_k:
                break
            if index in skip_indexes:
                continue
            randomize_list = [i]
            for jndex, j in enumerate(return_list[index + 1:]):
                if i['embedding_name'] == j['embedding_name']:
                    randomize_list.append(j)
                    skip_indexes.append(index + jndex + 1)
            if len(randomize_list) >= 2:
                random.shuffle(randomize_list)
                if 'hash' in return_type:
                    pack_id = i['obj']['pack_id']
                    hash_id = self.resource_pack_manager.available_packs.get(pack_id).get('manifest').get('contents').get('images').get('files').get(os.path.basename(i['path'])).get('hash')
                    return_list_2 += [[i['path'], hash_id] for i in pop_similar_images(randomize_list)]
                else:
                    return_list_2 += [[i['path'], hash_id] for i in pop_similar_images(randomize_list)]
            else:
                if 'hash' in return_type:
                    pack_id = i['obj']['pack_id']
                    hash_id = self.resource_pack_manager.available_packs.get(pack_id).get('manifest').get('contents').get('images').get('files').get(os.path.basename(i['path'])).get('hash')
                    return_list_2.append([i['path'], hash_id])
                else:
                    return_list_2.append(i['path'])




        return return_list_2
        # # 按相似度排序
        # similarities.sort(reverse=True)
        #
        # # 返回前top_k个结果
        # return [item[1] for item in similarities[:top_k]]



    def reload_resource_packs(self) -> None:
        """重新加载资源包"""
        self.resource_pack_manager = ResourcePackManager()
        self._try_load_cache()
        
    def enable_resource_pack(self, pack_id: str) -> bool:
        """启用资源包"""
        result = self.resource_pack_manager.enable_pack(pack_id)
        if result:
            self._try_load_cache()
        return result
        
    def disable_resource_pack(self, pack_id: str) -> bool:
        """禁用资源包"""
        result = self.resource_pack_manager.disable_pack(pack_id)
        if result:
            self._try_load_cache()
        return result
        
    def get_resource_packs(self) -> Dict[str, Dict]:
        """获取所有资源包"""
        return self.resource_pack_manager.get_available_packs()
        
    def get_enabled_resource_packs(self) -> Dict[str, Dict]:
        """获取所有启用的资源包"""
        return self.resource_pack_manager.get_enabled_packs()
        
    def get_resource_pack_cover(self, pack_id: str) -> Optional[str]:
        """获取资源包封面"""
        return self.resource_pack_manager.get_pack_cover(pack_id)


def pop_similar_images(input_image_list, threshold=0.9):
    return_images = []
    image_list = []
    for index, i in enumerate(input_image_list):
        c = i.copy()
        c['image'] = load_image(i['path'])
        image_list.append(c)
    for index, img in enumerate(image_list):

        max_similar = 0
        logger.trace(index)
        for j in image_list[index+1:]:
            max_similar = max(max_similar, calculate_image_similarity(img['image'], j['image']))
        if max_similar < threshold:
            return_images.append(img)

    return return_images

IMAGE_SEARCH_SERVICE = ImageSearch()