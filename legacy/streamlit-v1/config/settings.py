import inspect
import os, sys, shutil
import threading
from functools import cached_property
from typing import Dict, List, Optional
import yaml
from pydantic import Field, BaseModel
import typing as t

from config.utils import *

"""
修改配置：用with打开Config，修改后自动保存。可以修改多个配置。
with Config() as config:
    config.api.embedding_models.base_url = '123'
使用配置：每次使用前实例化Config
！！！注意：如果不重新实例化，config的值不会改变！！！
应当将Config视为一次性的实例。如果要缓存这个实例，应当保证这个缓存在使用的生命周期内，config不会被改变。
对于改不了的屎山代码，请在实例化Config时，将keep_tracked参数设置为True。注意：设置后，这个缓存将是只读的。

每次重新实例化不会重新读盘。在内存中有一个缓存。
"""

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.yaml')
CONFIG_EXAMPLE_FILE = os.path.join(CONFIG_DIR, 'config.example.yaml')

# 如果配置文件不存在,从示例文件复制
if not os.path.exists(CONFIG_FILE):
    shutil.copyfile(CONFIG_EXAMPLE_FILE, CONFIG_FILE)

# update config from example
c_old = load_yaml_file(CONFIG_FILE)
c_original = load_yaml_file(CONFIG_FILE)
c_example = load_yaml_file(CONFIG_EXAMPLE_FILE)
c_new = update_nested_dict(c_example, c_old)
if c_original != c_new:
    print('config.yaml updated')
    save_yaml_file(c_new, CONFIG_FILE)

global last_loaded_config
last_loaded_config = {}

class BaseConfig(BaseModel, frozen=False, extra='allow'):
    """Base config class."""

    def __init__(self, /, **data: t.Any):
        super().__init__(**data)
        self.__dict__['settled_dicts'] = []



    def get_changed_kv(self, my_key):
        # if len(self.__dict__['settled_keys']) > 0:
        #     return [[my_key, self.__dict__['settled_keys'][0]], self.__dict__['settled_values']]
        # for field in self.model_fields_set:
        #     if isinstance(self.__dict__[field], BaseConfig):
        #         r = self.__dict__[field].get_changed_kv(field)
        #         if r:
        #             r[0].insert(0, my_key)
        #             return r
        def append_my_key_to_keys(settled_dicts):
            for d in settled_dicts:
                d['key'].insert(0, my_key)
            return settled_dicts

        if len(self.__dict__['settled_dicts']) > 0:
            return append_my_key_to_keys(self.__dict__['settled_dicts'])

        for field in self.model_fields_set:
            if field in self.__dict__:
                if isinstance(self.__dict__[field], BaseConfig):
                    r = self.__dict__[field].get_changed_kv(field)
                    if r:
                        r = append_my_key_to_keys(r)
                        return r

    # def __setattr__(self, key, value):
    #     print(f"setattr:{key}-{value}")
    #     if 'settled_dicts' in self.__dict__:
    #         if not isinstance(value, BaseConfig):
    #             self.__dict__['settled_dicts'].append({
    #                 'key': [key],
    #                 'value': value
    #             })

# class ObserverdDict(dict):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.__dict__['settled_dicts'] = []
#     def __setitem__(self, key, value):
#         print(f"setitem:{key}-{value}")
#         if not isinstance(value, BaseConfig):
#             self.__dict__['settled_dicts'].append({
#                 'key': [key],
#                 'value': value
#             })

class EmbeddingModelConfig(BaseConfig):
    name: str
    performance: str

class VlmModelConfig(BaseConfig):
    name: str
    performance: str

class ModelsConfig(BaseConfig):
    embedding_models: Dict[str, EmbeddingModelConfig]
    selected_embedding_model: str
    vlm_models: Dict[str, VlmModelConfig]
    default_model: str

class PathsConfig(BaseConfig):
    image_dirs: Dict
    cache_file: str
    models_dir: str
    api_embeddings_cache_file: str
    label_images_cache_file: str
    resource_packs_dir: str = "resource_packs"

class OpenaiConfig(BaseConfig):
    base_url: str
    api_key: Optional[str] = None

class ApiConfig(BaseConfig):
    embedding_models: OpenaiConfig
    vlm_models: OpenaiConfig

class MiscConfig(BaseConfig):
    adapt_for_old_version: bool

class ResourcePackConfig(BaseConfig):
    enabled: bool = False
    path: Optional[str] = None
    type: Optional[str] = None
    cache_file: Optional[str] = None
class CommunityConfig(BaseConfig):
    manifest_urls: Dict[str, bool]

def update_nested_dict(dictionary, keys, value):
    """
    此函数用于在嵌套字典中根据给定的键路径更新值。

    :param dictionary: 要更新的嵌套字典
    :param keys: 一个包含键的列表，用于指定要更新的位置
    :param value: 要设置的新值
    :return: 更新后的字典
    """
    if len(keys) == 1:
        dictionary[keys[0]] = value
    else:
        key = keys[0]
        if key not in dictionary:
            dictionary[key] = {}
        update_nested_dict(dictionary[key], keys[1:], value)
    return dictionary


class ConfigCache:
    def __init__(self):
        self.config:dict|None = None
        self.modify_config_lock = threading.Lock()
    def set_config(self, config_):
        self.modify_config_lock.acquire()
        self.config = config_
        self.modify_config_lock.release()
    def del_config(self):
        self.modify_config_lock.acquire()
        self.config = None
        self.modify_config_lock.release()

    def get_config(self):
        if self.config is not None:
            self.modify_config_lock.acquire()
            c = self.config.copy()
            self.modify_config_lock.release()
            return c
        else:
            return False

config_cache = ConfigCache()

class Config(BaseConfig):
    api: ApiConfig
    models: ModelsConfig
    paths: PathsConfig
    misc: MiscConfig
    resource_packs: Dict[str, ResourcePackConfig] = {}
    community: CommunityConfig

    # CONFIG_SOURCES = [
    #     FileSource(
    #         file=CONFIG_FILE
    #     ),
    # ]



    def __init__(self, keep_tracked = False):
        data = config_cache.get_config()
        if not data:
            if TRACE_MODE:
                logger.trace('Loading config')
            data = load_yaml_file(CONFIG_FILE)
            config_cache.set_config(data)
        if keep_tracked:
            self.__dict__['keep_tracked'] = True
        super().__init__(**data)

    def __enter__(self):
        if 'keep_tracked' in self.__dict__ and self.__dict__['keep_tracked']:
            raise RuntimeError('Config object is being tracked, which cannot be modified.')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if sys.gettrace() is not None:
            logger.trace('Exiting the context')
        # r = self.get_changed_kv('config')
        # saving_dict = load_yaml_file(CONFIG_FILE)
        # for k_v in r:
        #     k_v['key'].pop(0)
        #     saving_dict = update_nested_dict(saving_dict, k_v['key'], k_v['value'])
        # if sys.gettrace() is not None:
        #     print(r)
        save_yaml_file(self.model_dump(), CONFIG_FILE)
        config_cache.del_config()

    # def __del__(self):
    #     print('Config object is being deleted')


    @property
    def base_dir(self) -> str:
        """获取项目根目录"""
        return os.path.dirname(os.path.dirname(__file__))

    def get_model_path(self, model_name: str) -> str:
        """获取模型保存路径"""
        return os.path.join(self.base_dir, self.paths.models_dir, model_name.replace('/', '_'))

    def get_abs_image_dirs(self) -> List[str]:
        """获取图片目录的绝对路径"""
        r = []
        for v in self.paths.image_dirs.values():
            if not os.path.isabs(v['path']):
                r.append(os.path.join(self.base_dir, v['path']))
            else:
                r.append(v['path'])

        return r

    def get_abs_cache_file(self) -> str:
        """获取缓存文件的绝对路径"""
        return os.path.join(self.base_dir, self.paths.cache_file)

    def get_abs_cover_cache_file(self) -> str:
        """获取封缓存文件的绝对路径"""
        return os.path.join(self.base_dir, self.paths.cover_cache)

    def get_abs_api_cache_file(self) -> str:
        """获取缓存文件的绝对路径"""
        return os.path.join(self.base_dir, self.paths.api_embeddings_cache_file)

    def get_label_images_cache_file(self) -> str:
        """获取缓存文件的绝对路径"""
        return os.path.join(self.base_dir,self.paths.label_images_cache_file)

    @cached_property
    def pack_embedding_cache_folder_path(self):
        p=os.path.join(self.base_dir, 'data', 'pack_embedding_cache')
        verify_folder(p)
        return p

    @cached_property
    def temp_dir(self) -> str:
        return os.path.join(self.base_dir, 'temp')

    def get_temp_path(self, name) -> str:
        _p = os.path.join(self.temp_dir, name)
        verify_folder(_p)
        return _p

    def __getattr__(self, item):
        if 'keep_tracked' in self.__dict__ and self.__dict__['keep_tracked']:
            return Config().__getattr__(item)
        else:
            return super().__getattr__(item)

    # def reload(self) -> None:
    #     """重新加载配置文件"""
    #     new_config = Config()
    #     self.api = new_config.api
    #     self.models = new_config.models
    #     self.paths = new_config.paths
    #     self.misc = new_config.misc

# 创建全局配置实例
# config = Config()


if __name__ == '__main__':
    """修改配置：用with打开Config，修改后自动保存。可以修改多个配置。"""
    with Config() as config:
        config.api.embedding_models.base_url = '123'
    """使用配置：每次使用前实例化Config"""
    """注意：如果不重新实例化，config的值不会改变！应当将Config视为一次新的实例。如果要缓存这个实例，应当保证config缓存在使用的生命周期内不会改变。"""
    print(Config().api.embedding_models.base_url)


