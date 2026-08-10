import os
import re
import sys
from loguru import logger
import typing as t
def verify_folder(root):
    if '.' in os.path.basename(root):
        root = os.path.dirname(root)
    if not os.path.exists(root):
        parent = os.path.dirname(root)
        if parent != root:  # 防止在根目录时无限递归
            verify_folder(parent)
        os.makedirs(root, exist_ok=True)
        print(f"dir {root} has been created")

TRACE_MODE = os.path.exists("trace") or os.path.exists("trace.txt") # 最高等级日志输出
DEBUG_MODE = sys.gettrace() is not None or TRACE_MODE # 调试模式

def remove_invalid_filename_chars(filename):
    # 定义正则表达式，匹配Windows文件名中不支持的字符
    pattern = r'[\\/*?:"<>|]'
    # 使用空字符串替换匹配到的字符
    return re.sub(pattern, ' ', filename)