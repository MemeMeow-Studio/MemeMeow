import yaml
import os, shutil
from pathlib import Path
from pydantic import BaseModel, validator

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'api_config.yaml')
CONFIG_EXAMPLE_FILE = os.path.join(CONFIG_DIR, 'api_config.example.yaml')

# 如果配置文件不存在,从示例文件复制
if not os.path.exists(CONFIG_FILE):
    shutil.copyfile(CONFIG_EXAMPLE_FILE, CONFIG_FILE)

class RateLimitConfig(BaseModel):
    enabled: bool
    requests: int
    window: int
    storage: str
    @validator('requests')
    def validate_requests(cls, v):
        if v < 1:
            raise ValueError("请求数必须大于0")
        return v
    
class APIModeConfig(BaseModel):
    default_api_key: str
    default_base_url: str

class UrlsConfig(BaseModel):
    return_type: str = "rel_path"
    path_replace_regex: str = ""
    url_prefix: str = ""
    url_postfix: str = ""

class APIConfig(BaseModel):
    protected_mode: bool
    allowed_endpoints: list[str]
    rate_limit: RateLimitConfig
    generate_cache: bool
    mode: str
    api_mode_config: APIModeConfig
    model: str
    urls: UrlsConfig



def load_config(config_path: str = "config/api_config.yaml") -> APIConfig:
    try:
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"配置文件 {config_path} 不存在")
            
        with open(config_file, encoding='utf-8') as f:
            print(f"正在加载配置文件: {config_path}")
            raw_config = yaml.safe_load(f)
            
        return APIConfig(**raw_config["api"])
        
    except Exception as e:
        raise RuntimeError(f"配置加载失败: {str(e)}")