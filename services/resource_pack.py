import os
import json
import shutil
import zipfile
from datetime import datetime
from typing import List, Dict, Optional

from config.settings import Config
from .utils import verify_folder, get_file_hash, download_file


class ResourcePackError(Exception):
    """TODO: 资源包相关错误处理"""
    print(f"ResourcePackError: {str(Exception)}")
    pass

class ResourcePackService:
    def __init__(self):
        self.export_dir = os.path.abspath("export")
        print(f"Export directory: {self.export_dir}")
        verify_folder(self.export_dir)
        
    def create_resource_pack(self, 
                           name: str,
                           version: str,
                           author: str,
                           description: str,
                           image_paths: List[str],
                           cover_image: Optional[str] = None,
                           tags: Optional[List[str]] = None) -> str:
        
        if not name or not version or not author:
            raise ResourcePackError("资源包名称、版本号和作者不能为空")
            
        if not image_paths:
            raise ResourcePackError("图片列表不能为空")
            
        valid_images = []
        for img_path in image_paths:
            if not os.path.exists(img_path):
                print(f"文件不存在: {img_path}")
                continue
            if not os.access(img_path, os.R_OK):
                print(f"文件无法访问: {img_path}")
                continue
            valid_images.append(img_path)
            
        if not valid_images:
            raise ResourcePackError("没有有效的图片文件可以打包")
            
        pack_dir = os.path.join(self.export_dir, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        print(f"Creating resource pack directory: {pack_dir}")
        try:
            verify_folder(pack_dir)
        except Exception as e:
            raise ResourcePackError(f"创建资源包目录失败: {str(e)}")
        
        images_dir = os.path.join(pack_dir, "images")
        try:
            verify_folder(images_dir)
        except Exception as e:
            raise ResourcePackError(f"创建图片目录失败: {str(e)}")
        
        # 处理封面图片
        cover_info = None
        if cover_image and os.path.exists(cover_image):
            try:
                cover_name = os.path.basename(cover_image)
                name_without_ext, ext = os.path.splitext(cover_name)
                ext = ext.lower()
                
                file_hash = get_file_hash(cover_image)
                if file_hash:
                    new_cover_name = f"cover{ext}"
                    new_cover_path = os.path.join(pack_dir, new_cover_name)
                    shutil.copy2(cover_image, new_cover_path)
                    print(f"复制封面: {cover_image} -> {new_cover_path}")
                    cover_info = {
                        "filename": new_cover_name,
                        "original_name": cover_name,
                        "hash": file_hash
                    }
            except Exception as e:
                print(str(e))
        
        copied_files = []
        file_mapping = {} 
        for img_path in valid_images:
            try:
                original_name = os.path.basename(img_path)
                name_without_ext, ext = os.path.splitext(original_name)
                ext = ext.lower()
                
                file_hash = get_file_hash(img_path)
                if not file_hash:
                    print(f"获取文件hash失败: {img_path}")
                    continue
                
                # 处理文件名重复问题
                new_name = original_name
                new_path = os.path.join(images_dir, new_name)
                
                if os.path.exists(new_path):
                    new_name = f"{name_without_ext}_{file_hash[:8]}{ext}"
                    new_path = os.path.join(images_dir, new_name)
                    
                shutil.copy2(img_path, new_path)
                print(f"复制文件: {img_path} -> {new_path}")
                copied_files.append(new_path)
                file_mapping[new_name] = {
                    "filepath": os.path.join("images/", new_name),
                    "hash": file_hash
                }
            except Exception as e:
                print(f"复制文件 {img_path} 失败: {str(e)}")
                continue
        if not copied_files:
            raise ResourcePackError("没有成功复制任何图片文件")
                
        manifest = {
            "name": name,
            "version": version,
            "author": author,
            "description": description,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "tags": tags or [],
            "cover": cover_info,
            "contents": {
                "images": {
                    "description": "图像资源目录",
                    "files": file_mapping
                }
            }
        }
        
        manifest_path = os.path.join(pack_dir, "manifest.json")
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=4)
            print(f"Created manifest.json: {manifest_path}")
        except Exception as e:
            raise ResourcePackError(f"创建manifest.json失败: {str(e)}")
            
        return pack_dir
        
    def export_resource_pack(self, pack_dir: str) -> str:
        if not os.path.exists(pack_dir):
            raise ResourcePackError(f"资源包目录不存在: {pack_dir}")
            
        if not os.path.isdir(pack_dir):
            raise ResourcePackError(f"指定的路径不是目录: {pack_dir}")
            
        if not os.path.exists(os.path.join(pack_dir, "manifest.json")):
            raise ResourcePackError("资源包目录中缺少manifest.json")
            
        images_dir = os.path.join(pack_dir, "images")
        if not os.path.exists(images_dir) or not os.path.isdir(images_dir):
            raise ResourcePackError("资源包目录中缺少images目录")
            
        if not os.listdir(images_dir):
            raise ResourcePackError("images目录为空")
            
        zip_name = os.path.basename(pack_dir)
        zip_path = os.path.join(os.path.dirname(pack_dir), f"{zip_name}.zip")
        print(f"Creating zip file: {zip_path}")
        
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(pack_dir):
                    for file in files:
                        # 使用绝对路径
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, pack_dir)
                        print(f"Adding to zip: {arcname}")
                        zf.write(file_path, arcname)
        except Exception as e:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            raise ResourcePackError(f"创建zip文件失败: {str(e)}")
    
        return zip_path

    def import_resource_pack(self, zip_file):
        target_dir = Config().paths.resource_packs_dir
        verify_folder(target_dir)
        target_dir = os.path.join(target_dir, os.path.splitext(os.path.basename(zip_file.name))[0])
        try:
            with zipfile.ZipFile(zip_file) as zip_ref:
                zip_ref.extractall(target_dir)
            print(f"成功解压 {zip_file.name} 到 {target_dir}")
        except Exception as e:
            raise ResourcePackError(f"解压ZIP文件失败: {str(e)}")

    def import_resource_pack_from_url(self, url, uuid=None):
        if uuid is None:
            target_file  = url.replace("https://", "").replace("http://", "").replace("/", "_").replace("\\", "_")
        else:
            target_file = f"{uuid}.json"
        target_dir = os.path.join(Config().paths.resource_packs_dir, os.path.splitext(target_file)[0])
        verify_folder(target_dir)
        return download_file(url, os.path.join(target_dir, "manifest.json"))


RESOURCE_PACK_SERVICE = ResourcePackService()

