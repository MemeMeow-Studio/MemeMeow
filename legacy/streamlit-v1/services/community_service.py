import os
import threading
import time
import json
import requests

import numpy as np
import pickle
import re
from typing import Optional, List, Dict

from config.settings import Config
from stpages.utils import ENDWITH_IMAGE

from services.embedding_service import EmbeddingService
from services.resource_pack import RESOURCE_PACK_SERVICE
from services.resource_pack_manager import RESOURCE_PACK_MANAGER
from services.utils import *
from services.llm_enhance import LLMEnhance
from services.cache_service import CacheService

"""manifest template:
{
"community_info": {
"resource_url": "https://github.com/MemeMeow-Studio/Memes-Community/raw/main",
"update_url": "https://github.com/MemeMeow-Studio/Memes-Community/raw/main/community_manifest.json",
"timestamp": 1747738810
},
"meme_libs": {
"4946f03b-11ae-4b03-81d5-db74dbf3f853": {
"name": "《这就是中国》大全",
"version": "1.0.1",
"author": "Official",
"description": "none",
"created_at": "2025-05-19",
"timestamp": 1747667458,
"tags": [],
"url": "https://raw.githubusercontent.com/MemeMeow-Studio/VVfromVideo/main/all_vvs",
"update_url": "https://raw.githubusercontent.com/MemeMeow-Studio/VVfromVideo/main/all_vvs/manifest.json",
"uuid": "4946f03b-11ae-4b03-81d5-db74dbf3f853"
},
"13e30784-6ebf-4c17-9139-aafe95719c61": {
"name": "精选v图",
"version": "1.0.1",
"author": "Official",
"description": "none",
"created_at": "2025-05-19",
"timestamp": 1747667458,
"tags": [],
"url": "https://raw.githubusercontent.com/MemeMeow-Studio/VVfromVideo/main/精选表情",
"update_url": "https://raw.githubusercontent.com/MemeMeow-Studio/VVfromVideo/main/精选表情/manifest.json",
"uuid": "13e30784-6ebf-4c17-9139-aafe95719c61"
}
}
}

"""

class CommunityService:
    def __init__(self):
        self.all_community_repos_info = []
        self.all_manifest_path = os.path.join(Config().temp_dir, "all_manifest.json")
        self.reload_community_info()


    def update_local_manifests(self):
        for community_info in self.all_community_repos_info:
            local_manifest = RESOURCE_PACK_MANAGER.enabled_packs.get(community_info.get("uuid"), None)
            # 如果本地没有manifest，或者本地的manifest比远程的旧，则下载远程的manifest
            if not local_manifest or community_info.get("timestamp") < local_manifest.get("timestamp"):
                RESOURCE_PACK_SERVICE.import_resource_pack_from_url(community_info.get("update_url"), uuid=community_info.get("uuid"))

    def reload_community_info(self):
        self.all_community_repos_info = []
        if not os.path.exists(self.all_manifest_path):
            self.download_and_compose_all_manifests()

        try:
            with open(self.all_manifest_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
                meme_libs = manifest_data.get('meme_libs', {})
                self.all_community_repos_info = list(meme_libs.values())
        except Exception as e:
            print(f"Error loading manifest file: {str(e)}")
            self.all_community_repos_info = []

    def download_and_compose_all_manifests(self):
        composed_manifest = {}
        manifest_urls = Config().community.manifest_urls
        latest_timestamp = 0
        for url in manifest_urls:
            try:
                # 下载manifest文件
                response = requests.get(url)
                if response.status_code != 200:
                    print(f"Failed to download manifest from {url}, status code: {response.status_code}")
                    continue
                
                # 解析manifest内容
                manifest_data = response.json()
                
                if 'community_info' in manifest_data:
                    community_info = manifest_data['community_info']
                    # 更新最新的timestamp
                    if community_info.get('timestamp', 0) > latest_timestamp:
                        latest_timestamp = community_info['timestamp']
                    
                    # # 检查是否存在resource_url和update_url
                    # if 'resource_url' in community_info and 'update_url' in community_info:
                    #     # 更新或添加到composed_manifest
                    #     composed_manifest['community_info'] = {
                    #         "resource_url": community_info['resource_url'],
                    #         "update_url": community_info['update_url'],
                    #         "timestamp": latest_timestamp
                    #     }
                
                # 处理meme_libs部分
                if 'meme_libs' in manifest_data:
                    for uuid, lib_info in manifest_data['meme_libs'].items():
                        # 检查是否存在该UUID
                        if uuid not in composed_manifest.get('meme_libs', {}):
                            # UUID不存在，添加新条目
                            if 'meme_libs' not in composed_manifest:
                                composed_manifest['meme_libs'] = {}
                            composed_manifest['meme_libs'][uuid] = lib_info
                        else:
                            # UUID存在，比较timestamp
                            existing_timestamp = composed_manifest['meme_libs'][uuid].get('timestamp', 0)
                            new_timestamp = lib_info.get('timestamp', 0)
                            
                            if new_timestamp > existing_timestamp:
                                # 新条目timestamp更大，替换
                                composed_manifest['meme_libs'][uuid] = lib_info
            except Exception as e:
                print(f"Error processing manifest from {url}: {str(e)}")
                
        composed_manifest['community_info'] = {
            "timestamp": latest_timestamp
        }
        
        # 保存合并后的manifest到文件
        try:
            os.makedirs(os.path.dirname(self.all_manifest_path), exist_ok=True)
            with open(self.all_manifest_path, 'w', encoding='utf-8') as f:
                json.dump(composed_manifest, f, ensure_ascii=False, indent=2)
            print(f"Saved composed manifest to {self.all_manifest_path}")
        except Exception as e:
            print(f"Error saving composed manifest: {str(e)}")
        
        return composed_manifest



