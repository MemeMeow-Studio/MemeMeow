import yaml
from base import *

def update_nested_dict(original, new):
    """
    递归更新嵌套字典。

    :param original: 原始的嵌套字典
    :param new: 用于更新的新嵌套字典
    :return: 更新后的嵌套字典
    """
    for key, value in new.items():
        if key in original and isinstance(original[key], dict) and isinstance(value, dict):
            # 如果键存在且对应的值都是字典，则递归更新
            original[key] = update_nested_dict(original[key], value)
        else:
            # 否则，直接更新键值对
            original[key] = value
    return original

def load_yaml_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        data = yaml.safe_load(file)
    return data

def save_yaml_file(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as file:
        yaml.dump(data, file, allow_unicode=True, default_flow_style=False)