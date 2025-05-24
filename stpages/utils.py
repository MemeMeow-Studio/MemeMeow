import os

import cv2
from config.settings import Config
from base import *
ENDWITH_IMAGE = ['.jpg', '.jpeg', '.png', '.gif']



def get_all_file_paths(folder_path, endwith=None):
    # 用于存储所有文件的绝对路径
    file_paths = []
    # 使用os.walk()遍历文件夹及其子文件夹
    for root, directories, files in os.walk(folder_path):
        for filename in files:
            if endwith:
                if not os.path.splitext(os.path.basename(filename))[1] in endwith:
                    continue
            # 构建文件的绝对路径
            file_path = os.path.join(root, filename)
            # 将绝对路径添加到列表中
            file_paths.append(file_path)
    return file_paths

def resize_image(img, max_size=1024):
    """尺寸调整"""
    h, w = img.shape[:2]

    if max(h, w) > max_size:
        scale = max_size / max(h, w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return img

IMAGE_DIRS_PATH = 'data/image_dirs'
if not os.path.exists(IMAGE_DIRS_PATH):
    os.makedirs(IMAGE_DIRS_PATH)

# 获取image_dirs下的所有文件夹
def get_image_dirs():
    dirs = ['data/images']  # 默认包含原始图片目录
    if os.path.exists(IMAGE_DIRS_PATH):
        for item in os.listdir(IMAGE_DIRS_PATH):
            item_path = os.path.join(IMAGE_DIRS_PATH, item)
            if os.path.isdir(item_path):
                dirs.append(item_path)
    for i in Config().paths.image_dirs.values():
        dirs.append(i['path'])
    return dirs