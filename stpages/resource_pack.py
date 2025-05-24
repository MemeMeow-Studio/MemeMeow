import time
import streamlit as st
from config.settings import Config
from services.resource_pack import RESOURCE_PACK_SERVICE
from services.image_search import IMAGE_SEARCH_SERVICE
from services.community_service import CommunityService
import requests
import threading
import asyncio

# """初始化资源包管理相关的session state"""
if 'search_engine' not in st.session_state:
    st.session_state.search_engine = IMAGE_SEARCH_SERVICE
if 'has_cache' not in st.session_state:
    st.session_state.has_cache = st.session_state.search_engine.has_cache()
if 'upload_file_key' not in st.session_state:
    st.session_state.upload_file_key = int(time.time()*100)
if "pack_url" not in st.session_state:
    st.session_state.pack_url = ""

def on_enable_resource_pack(pack_id):
    """启用资源包回调"""
    if st.session_state.search_engine.enable_resource_pack(pack_id):
        st.success(f"已启用资源包")
        # 更新缓存状态
        st.session_state.has_cache = st.session_state.search_engine.has_cache()
    else:
        st.error(f"启用资源包失败")

def on_disable_resource_pack(pack_id):
    """禁用资源包回调"""
    if st.session_state.search_engine.disable_resource_pack(pack_id):
        st.success(f"已禁用资源包")
        # 更新缓存状态
        st.session_state.has_cache = st.session_state.search_engine.has_cache()
    else:
        st.error(f"禁用资源包失败")

# """重新加载资源包回调"""
st.session_state.search_engine.reload_resource_packs()
st.success("已重新扫描资源包")
# 更新缓存状态
st.session_state.has_cache = st.session_state.search_engine.has_cache()

# """渲染资源包管理界面"""
# initialize_state()

st.title("资源包管理")

# 创建左右两栏布局
left_col, right_col = st.columns(2)

def on_generate_cache():
    """生成缓存回调"""
    with st.spinner('正在生成表情包缓存...'):
        progress_bar = st.progress(0)
        st.session_state.search_engine.generate_cache(progress_bar)
        progress_bar.empty()
        # 强制重新检查缓存状态
        st.session_state.has_cache = st.session_state.search_engine.has_cache()
    st.success('缓存生成完成！')

def on_reload_resource_packs():
    """重新加载资源包回调"""
    st.session_state.search_engine.reload_resource_packs()
    st.success("已重新加载资源包")
    # 更新缓存状态
    st.session_state.has_cache = st.session_state.search_engine.has_cache()

with left_col:
    st.header("本地资源包")

    # 生成缓存按钮
    has_cache = st.session_state.search_engine.has_cache()
    if not has_cache:
        st.warning(f"⚠️ 尚未生成表情包缓存, 当前模型：{st.session_state.search_engine.get_model_name()}")



    # 加载资源包
    files = st.file_uploader("导入本地资源包",
                        type=["zip"],
                                accept_multiple_files=True,
                                key=st.session_state.upload_file_key)
    if files:
        for file in files:
            # 解压资源包到resource_packs目录
            RESOURCE_PACK_SERVICE.import_resource_pack(file)
            st.success(f"导入资源包 {file.name} 成功")
        st.session_state.upload_file_key = int(time.time()*100)
        
    col1, col2 = st.columns([3, 1])
    with col1:
        st.text_input("请输入资源包URL", key="pack_url")
    with col2:
        if st.button("导入在线资源包", use_container_width=True):
            if st.session_state.pack_url:
                if RESOURCE_PACK_SERVICE.import_resource_pack_from_url(st.session_state.pack_url):
                    st.success(f"导入资源包 {st.session_state.pack_url} 成功")
                else:
                    st.error(f"导入资源包 {st.session_state.pack_url} 失败")
                st.session_state.pack_url = ""
            else:
                st.warning("请输入有效的资源包URL")

    # 重新加载资源包按钮
    st.button(
        "重新扫描资源包",
        on_click=on_reload_resource_packs,
        help="重新扫描resource_packs目录，加载新的资源包",
        key="reload_resource_packs_btn",
        use_container_width=True
    )

    # 显示缓存生成按钮
    if st.button(
            "重新生成缓存" if has_cache else "生成表情包缓存",
            help="更新表情包缓存" if has_cache else "首次使用需要生成表情包缓存",
            key="generate_cache_btn",
            use_container_width=True
    ):
        on_generate_cache()

    # 获取所有资源包
    resource_packs = st.session_state.search_engine.get_resource_packs()
    enabled_packs = st.session_state.search_engine.get_enabled_resource_packs()

    if not resource_packs:
        st.info("没有找到资源包，请将资源包解压到resource_packs目录")
    else:
        st.write(f"找到 {len(resource_packs)} 个资源包，已启用 {len(enabled_packs)} 个")
        
        # 显示资源包列表
        for pack_id, pack_info in resource_packs.items():
            with st.container():
                col1, col2 = st.columns([3, 1])
                
                with col1:
                    # 获取封面图片
                    cover_path = st.session_state.search_engine.get_resource_pack_cover(pack_id)
                    if cover_path:
                        st.image(cover_path, width=64)
                        
                    st.write(f"**{pack_info['name']}** v{pack_info['version']}")
                    st.caption(f"作者: {pack_info['author']}")
                    if pack_info.get('description'):
                        st.caption(pack_info['description'])
                    
                    # 显示缓存状态
                    cache_generated = st.session_state.search_engine.resource_pack_manager.is_pack_cache_generated(
                        pack_id, 
                        st.session_state.search_engine.embedding_service.selected_embedding_model
                    )
                    if cache_generated:
                        st.success("缓存已生成", icon="✅")
                    else:
                        st.warning("缓存未生成", icon="⚠️")
                
                with col2:
                    if pack_info['enabled']:
                        if not pack_info.get('is_default', False):
                            st.button(
                                "禁用",
                                key=f"disable_{pack_id}",
                                on_click=on_disable_resource_pack,
                                args=(pack_id,),
                                use_container_width=True
                            )
                        else:
                            st.write("默认资源包")
                    else:
                        st.button(
                            "启用",
                            key=f"enable_{pack_id}",
                            on_click=on_enable_resource_pack,
                            args=(pack_id,),
                            use_container_width=True
                        )
                
                st.divider()

with right_col:
    st.header("社区资源包")
    if st.button("更新社区资源包"):
        CommunityService().update_local_manifests()
    urls_config = Config().community.manifest_urls
    for k, v in urls_config.items():
        col1, col2, col3 = st.columns([3, 1, 1])
        with col1:
            st.write(f"**{k}**")
            st.write(f"enabled: {v}")
            def check_url_access(url, status_container):
                try:
                    response = requests.head(url, timeout=3)
                    if response.status_code == 200:
                        status_container.success("可访问", icon="✅")
                    else:
                        status_container.warning(f"状态码: {response.status_code}", icon="⚠️")
                except Exception:
                    status_container.error("无法访问", icon="❌")

            status_container = st.empty()
            check_url_access(
                k,
                status_container)
            # threading.Thread(target=check_url_access, args=(k, status_container)).start()
            
        with col2:
            btn_label = "禁用" if v else "启用"
            
            if st.button(btn_label, key=f"community_toggle_{k}", use_container_width=True):
                with Config() as config:
                    config.community.manifest_urls[k] = not v
                st.rerun()
        with col3:
            if st.button("删除", key=f"community_delete_{k}", use_container_width=True):
                with Config() as config:
                    del config.community.manifest_urls[k]
                st.rerun()
    with st.expander("添加社区资源包URL"):
        new_url = st.text_input("请输入新的社区资源包URL", key="new_community_url")
        if st.button("添加URL", key="add_community_url_btn", use_container_width=True):
            if new_url:
                with Config() as config:
                    config.community.manifest_urls[new_url] = True
                st.success("添加成功")
                st.rerun()
            else:
                st.warning("请输入有效的URL")

    
    
