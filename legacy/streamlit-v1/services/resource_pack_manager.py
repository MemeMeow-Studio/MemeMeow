import os
import json
import pickle
import shutil
from typing import Dict, List, Optional, Tuple
from PIL import Image, ImageDraw
import numpy as np

from config.settings import Config, ResourcePackConfig
from services.utils import verify_folder, get_file_hash
from base import *

class ResourcePackManager:
    """资源包管理器，负责加载、解析和缓存资源包"""
    
    def __init__(self):
        """初始化资源包管理器"""
        self.resource_packs_dir = os.path.join(Config().base_dir, Config().paths.resource_packs_dir)
        verify_folder(self.resource_packs_dir)
        
        # 存储所有可用的资源包信息
        self.available_packs: Dict[str, Dict] = {}
        # 存储已启用的资源包信息
        self.enabled_packs: Dict[str, Dict] = {}
        
        # 加载所有资源包
        self._load_resource_packs()
        
    def _load_resource_packs(self) -> None:
        """加载所有资源包信息"""
        # 清空当前资源包信息
        self.available_packs = {}

        # 遍历resource_packs目录，加载所有资源包
        if os.path.exists(self.resource_packs_dir):
            for item in os.listdir(self.resource_packs_dir):
                pack_dir = os.path.join(self.resource_packs_dir, item)
                if os.path.isdir(pack_dir):
                    manifest_path = os.path.join(pack_dir, "manifest.json")
                    if os.path.exists(manifest_path):
                        try:
                            with open(manifest_path, "r", encoding="utf-8") as f:
                                manifest = json.load(f)
                                
                            # 检查资源包是否有效
                            if not self._validate_resource_pack(pack_dir, manifest):
                                continue

                            resource_config = Config().resource_packs.get(f'pack_{item}', ResourcePackConfig())


                            # 获取封面图片路径
                            cover_path = None
                            if manifest.get("cover") and manifest["cover"].get("filename"):
                                cover_file = manifest["cover"]["filename"]
                                cover_path = os.path.join(pack_dir, cover_file)
                                if not os.path.exists(cover_path):
                                    cover_path = None
                            
                            # 构建资源包信息
                            pack_id = f"pack_{item}"

                            self.available_packs[pack_id] = {
                                "name": manifest.get("name", item),
                                "version": manifest.get("version", "1.0.0"),
                                "author": manifest.get("author", "Unknown"),
                                "description": manifest.get("description", ""),
                                "path": pack_dir,
                                "type": "vv",  # 默认类型
                                "cache_file": self.get_pack_cache_file(pack_id),
                                "enabled": resource_config.enabled,  # 默认不启用
                                "is_default": False,
                                "cover": cover_path,
                                "manifest": manifest,
                                "pack_dir": pack_dir,
                                "url": manifest.get("url", ""),
                            }
                            if manifest.get('regex') is not None:
                                self.available_packs[pack_id]['regex'] = manifest.get('regex')
                            if resource_config.enabled:
                                self.enabled_packs[pack_id] = self.available_packs[pack_id]
                        except Exception as e:
                            logger.error(f"加载资源包 {item} 失败: {e}")
    
    def _validate_resource_pack(self, pack_dir: str, manifest: Dict) -> bool:
        """验证资源包是否有效"""
        # 检查必要的字段
        if not manifest.get("name") or not manifest.get("version") or not manifest.get("author"):
            print(f"资源包 {pack_dir} 缺少必要的字段")
            return False
            
        # 检查images目录是否存在
        # images_dir = os.path.join(pack_dir, "images")
        # if not os.path.exists(images_dir) or not os.path.isdir(images_dir):
        #     print(f"资源包 {pack_dir} 缺少images目录")
        #     return False
        #
        # # 检查images目录是否有图片
        # has_images = False
        # for root, _, files in os.walk(images_dir):
        #     for file in files:
        #         if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
        #             has_images = True
        #             break
        #     if has_images:
        #         break
        #
        # if not has_images:
        #     print(f"资源包 {pack_dir} 的images目录中没有图片")
        #     return False
            
        return True
    
    def get_available_packs(self) -> Dict[str, Dict]:
        """获取所有可用的资源包"""
        return self.available_packs
    
    def get_enabled_packs(self) -> Dict[str, Dict]:
        """获取所有已启用的资源包"""
        return self.enabled_packs
    
    def enable_pack(self, pack_id: str) -> bool:
        """启用指定的资源包"""
        if pack_id in self.available_packs and not self.available_packs[pack_id]["enabled"]:
            self.available_packs[pack_id]["enabled"] = True
            self.enabled_packs[pack_id] = self.available_packs[pack_id]
            
            # 更新配置文件
            with Config() as config:
                # if "resource_packs" not in config.__dict__:
                #     config.resource_packs = {}
                if pack_id not in config.resource_packs:
                    config.resource_packs[pack_id] = ResourcePackConfig(enabled=True)
                else:
                    config.resource_packs[pack_id].enabled = True
            
            return True
        return False
    
    def disable_pack(self, pack_id: str) -> bool:
        """禁用指定的资源包"""
        if pack_id in self.available_packs and self.available_packs[pack_id]["enabled"]:
            self.available_packs[pack_id]["enabled"] = False
            if pack_id in self.enabled_packs:
                del self.enabled_packs[pack_id]
            
            # 更新配置文件
            with Config() as config:
                if "resource_packs" in config.__dict__ and pack_id in config.resource_packs:
                    config.resource_packs[pack_id].enabled = False
            
            return True
        return False
    
    def get_pack_cover(self, pack_id: str, size: Tuple[int, int] = (512, 512)) -> Optional[str]:
        """获取资源包的封面图片路径，如果没有封面则生成一个默认封面"""
        if pack_id not in self.available_packs:
            return None
            
        pack_info = self.available_packs[pack_id]
        
        # 如果有封面，直接返回
        if pack_info.get("cover") and os.path.exists(pack_info["cover"]):
            return pack_info["cover"]
            
        # 生成默认封面
        cover_cache_dir = Config().get_abs_cover_cache_file()
        verify_folder(cover_cache_dir)
        
        default_cover_path = os.path.join(cover_cache_dir, f"{pack_id}_cover.png")
        
        # 如果已经生成过默认封面，直接返回
        if os.path.exists(default_cover_path):
            return default_cover_path
            
        # 生成一个灰色的封面，并添加资源包名称
        img = Image.new('RGB', size, color=(200, 200, 200))
        draw = ImageDraw.Draw(img)
        
        # 添加资源包名称
        pack_name = pack_info.get("name", "未命名资源包")
        text_width = draw.textlength(pack_name, font=None)
        text_position = ((size[0] - text_width) / 2, size[1] / 2)
        draw.text(text_position, pack_name, fill=(0, 0, 0))
        
        # 保存封面
        img.save(default_cover_path)
        
        return default_cover_path
    
    def get_cache_files(self) -> Dict[str, str]:
        """获取所有启用的资源包的缓存文件路径"""
        cache_files = {}
        for pack_id, pack_info in self.enabled_packs.items():
            cache_file = pack_info["cache_file"]
            cache_files[pack_id] = cache_file
        return cache_files
    
    def is_pack_cache_generated(self, pack_id: str, model_name) -> bool:
        """检查指定资源包的缓存是否已生成"""
        if pack_id not in self.available_packs:
            print(f"资源包 {pack_id} 不存在")
            return False
            
        pack_info = self.available_packs[pack_id]
        cache_file = pack_info["cache_file"]
            
        # 打印调试信息
        exists = os.path.exists(cache_file)
        print(f"检查缓存文件: {cache_file}, 模型: {model_name}, 存在: {exists}")
            
        return exists
        
    def get_pack_cache_file(self, pack_id: str, model_name: Optional[str] = None) -> Optional[str]:
        """获取指定资源包的缓存文件路径"""
        fp = os.path.join(Config().pack_embedding_cache_folder_path, f"{pack_id}.pkl")
        verify_folder(fp)
        return fp

RESOURCE_PACK_MANAGER = ResourcePackManager()