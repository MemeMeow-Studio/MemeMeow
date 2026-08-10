# Streamlit v1 只读归档

这里保存 FastAPI + Vue 迁移前的 Streamlit 实现、旧配置体系、旧服务层、历史限流中间件和默认资源包素材，仅用于查阅、行为对比和迁移追溯。

## 使用边界

- 该目录不参与 FastAPI 启动、Vue 构建、测试或 Docker 镜像构建。
- 不要从新代码导入这里的模块，也不要在这里继续开发功能。
- 旧代码依赖的配置、模型缓存和 Streamlit 运行环境不再由主项目维护。
- 当前迁移前的完整 Git 基线由标签 `streamlit-v1` 保留；本目录是工作树中的便捷参考副本。

## 内容

- `stpages/`：旧 Streamlit 页面
- `services/`：旧搜索、嵌入、VLM、资源包和社区同步服务
- `config/`：旧 YAML 配置和配置辅助代码
- `middleware/`：旧保护模式与限流实现
- `assets/default_pack/`：历史资源包素材，不再作为生产资源包加载
- `screenshots/`：旧界面截图

主项目运行入口现在是根目录的 `api.py`，前端位于 `frontend/`，服务端运行代码位于 `backend/`。
