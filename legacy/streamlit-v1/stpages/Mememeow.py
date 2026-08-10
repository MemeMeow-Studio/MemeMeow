import time

import streamlit as st
import random
import yaml
from services.image_search import IMAGE_SEARCH_SERVICE
from config.settings import Config

# 页面配置
try:
    st.set_page_config(
        page_title="Mememeow",
        page_icon="🐱",
        layout="wide",
        initial_sidebar_state="expanded"
    )
except Exception as e:
    pass

def save_config_yaml(api_key: str, base_url: str) -> None:
    """保存API key到config.yaml"""
    config_path = 'config/config.yaml'
    try:
        # 更新API key
        with Config() as config_data:
            config_data.api.embedding_models.api_key = api_key
            config_data.api.embedding_models.base_url = base_url
        # 更新EmbeddingService的API key
        if st.session_state.search_engine:
            st.session_state.search_engine.embedding_service.embedding_api_key = api_key
            st.session_state.search_engine.embedding_service.base_url = base_url
    except Exception as e:
        st.error(f"保存配置失败: {str(e)}")

# 搜索框提示语列表
SEARCH_PLACEHOLDERS = [
    "如何看待Deepseek？",
    "如何看待六代机？",
    "如何看待Mememeow？",
    "如何看待张维为？",
    "如何看待...？",
]

st.title("Mememeow")

# 初始化session state
if 'placeholder' not in st.session_state:
    st.session_state.placeholder = random.choice(SEARCH_PLACEHOLDERS)
if 'search_query' not in st.session_state:
    st.session_state.search_query = ""
if 'n_results' not in st.session_state:
    st.session_state.n_results = 5
if 'api_key' not in st.session_state:
    st.session_state.embedding_api_key = Config().api.embedding_models.api_key
    if st.session_state.embedding_api_key is None:
        st.session_state.embedding_api_key = ''
if 'base_url' not in st.session_state:
    st.session_state.base_url = Config().api.embedding_models.base_url
    if st.session_state.base_url is None:
        st.session_state.base_url = ''
if 'search_engine' not in st.session_state:
    st.session_state.search_engine = IMAGE_SEARCH_SERVICE
if 'has_cache' not in st.session_state:
    st.session_state.has_cache = st.session_state.search_engine.has_cache()
if 'show_resource_packs' not in st.session_state:
    st.session_state.show_resource_packs = False
if 'enable_llm_enhance' not in st.session_state:
    st.session_state.enable_llm_enhance = False

# 搜索函数
def search():
    if not st.session_state.search_query:
        st.session_state.results = []
        return []
        
    try:
        with st.spinner('Searching'):
            results = st.session_state.search_engine.search(
                st.session_state.search_query, 
                st.session_state.n_results,
                api_key = st.session_state.embedding_api_key,
                use_llm = st.session_state.enable_llm_enhance
            )
            st.session_state.results = results if results else []
            return st.session_state.results
    except Exception as e:
        st.sidebar.error(f"搜索失败: {e}")
        st.session_state.results = []
        return []

# 回调函数
def on_input_change():
    st.session_state.results = []
    st.session_state.search_query = st.session_state.user_input
    if st.session_state.search_query:
        st.session_state.results = search()

def on_slider_change():
    st.session_state.n_results = st.session_state.n_results_widget
    if st.session_state.search_query:
        st.session_state.results = search()

def on_api_key_change():
    new_key = st.session_state.api_key_input
    if new_key != st.session_state.embedding_api_key:
        st.session_state.embedding_api_key = new_key
        # 保存到配置文件
        save_config_yaml(new_key, st.session_state.base_url)
        
def on_base_url_change():
    new_base_url = st.session_state.base_url_input
    if new_base_url != st.session_state.base_url:
        st.session_state.base_url = new_base_url
        # 保存到配置文件
        save_config_yaml(st.session_state.embedding_api_key, new_base_url)

def on_generate_cache():
    """生成缓存回调"""
    with st.spinner('正在生成表情包缓存...'):
        progress_bar = st.progress(0)
        st.session_state.search_engine.generate_cache(progress_bar)
        progress_bar.empty()
        # 强制重新检查缓存状态
        st.session_state.has_cache = st.session_state.search_engine.has_cache()
    st.success('缓存生成完成！')

def on_toggle_resource_packs():
    """切换资源包面板显示状态"""
    st.session_state.show_resource_packs = not st.session_state.show_resource_packs

# 侧边栏搜索区域
with st.sidebar:
    st.title("🐱 MemeMeow")
    # API密钥输入(仅API模式)

    api_key = st.text_input(
        "请输入API Key",
        value=st.session_state.embedding_api_key,
        type="password",
        key="api_key_input",
        on_change=on_api_key_change
    )
    base_url = st.text_input(
        "请输入Base URL",
        value=st.session_state.base_url,
        key="base_url_input",
        on_change=on_base_url_change
    )

    # 生成缓存按钮
    has_cache = st.session_state.search_engine.has_cache()
    if not has_cache:
        st.warning(f"⚠️ 尚未生成表情包缓存, 当前模型：{st.session_state.search_engine.get_model_name()}")
    
    # 显示缓存生成按钮

    button_text = "重新生成缓存" if has_cache else "生成表情包缓存"
    help_text = "更新表情包缓存" if has_cache else "首次使用需要生成表情包缓存"

    if st.button(
        button_text,
        help=help_text,
        key="generate_cache_btn",
        use_container_width=True
    ):
        on_generate_cache()
    
    # 检查是否可以进行搜索
    can_search = has_cache

    user_input = st.text_input(
        "请输入搜索关键词", 
        value=st.session_state.search_query,
        placeholder=st.session_state.placeholder,
        key="user_input",
        on_change=on_input_change,
        disabled=not can_search
    )
    
    n_results = st.slider(
        "选择展示的结果数量", 
        1, 30, 
        value=st.session_state.n_results,
        key="n_results_widget",
        on_change=on_slider_change
    )

    st.checkbox("启用llm搜索增强",
                key='enable_llm_enhance')

# 主区域显示搜索结果
if 'results' in st.session_state and st.session_state.results:
    # 计算每行显示的图片数量
    cols = st.columns(3)
    for idx, img_path in enumerate(st.session_state.results):
        with cols[idx % 3]:
            st.image(img_path)
elif st.session_state.search_query:
    st.info("未找到匹配的表情包")

# 添加页脚
st.markdown("---")
st.markdown(
    """
    <div style='text-align: center'>
    
    🌟 关注我 | Follow Me 🌟
    
    👨‍💻 [GitHub](https://github.com/MemeMeow-Studio) · 
    📺 [哔哩哔哩](https://space.bilibili.com/165404794) · 
    📝 [博客](https://www.xy0v0.top/)
    </div>
    """, 
    unsafe_allow_html=True
)