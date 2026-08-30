# 高级部署与架构指南

本文档包含 MemeMeow 的底层运行机制、权限控制机制、组件网络边界以及本地开发指南。适合需要进行二次开发、深度运维或了解系统架构的开发者阅读。对于常规部署，请参考项目根目录的 `README.md`。

## 1. 容器运行身份与持久化权限

`start.sh start` 默认把当前服务用户的 `id -u`/`id -g` 导出为 `MEMEMEOW_RUNTIME_UID`/`MEMEMEOW_RUNTIME_GID`，也可以显式设置这两个变量覆盖自动值。两者必须是非 root 正整数；以 root 启动入口会在 Compose 创建服务前失败。直接运行 `docker compose --profile app up` 时必须在环境或 `.env` 中显式填写同一组身份，否则 Compose 插值校验失败。完整迁移、排障和回滚顺序请参考 `docs/runtime-identity.md`。

每次启动会先运行一次性的 `runtime-init` 容器：
- 它只挂载图片根、Agent runtime volume 和 executor token volume，拒绝符号链接、特殊节点和多链接普通文件。
- 仅兼容留在同一 `node_modules` 树内、目标为普通文件的 npm `.bin` 相对链接。
- 把受控目录设为 `0700`、普通文件设为 `0600` 后归属目标 UID/GID。
- 历史图片只改变所有权和权限，不改变文件字节。

**注意**：初始化失败时 API 与 Agent 不会启动。回滚前应停止新版本并保留 volume 备份，旧版本若再次以 root 写入新图片会重新产生权限冲突，不能把回滚当作长期部署方案。

## 2. Agent 执行器 (Executor) 与安全边界

### 镜像与挂载
镜像位于 `docker/agent/Dockerfile`，预装 OpenCode、Node、Python、Bash、curl、jq、file、ImageMagick、ffmpeg、Tesseract 中英文 OCR 和常见文本工具。
- 镜像默认用户本身是非 root；Compose 会以部署提供的 UID/GID 覆盖运行身份。
- 入口是固定的 `executor.server` HTTP 服务。
- 只读挂载 `data/images` 和 `skills/research-meme-context`。
- 读写挂载 named volume `mememeow-agent-runtime-data:/runtime`，并在独立的 `mememeow-agent-executor-secret` named volume 中以 0600 权限持久化首次生成的随机 executor token。

### 运行时边界
Agent 的 HOME、workspace 和任务结果都在初始化过的 runtime volume 中，不依赖镜像内固定用户的 home 所有权。
- API 以只读方式读取同一 token 文件，不会把 token 写入 checkout、`.env`、日志、结果文件或 OpenCode 子进程环境。
- 容器不会挂载项目根目录、数据库凭据、用户目录或 Docker socket。
- 后端只向 `http://mememeow-agent-runtime:8277` 发送带 token 的结构化任务。
- 每个任务使用独立 OpenCode session 和 `task-results/<task_id>/` 结果目录。
- 反向图片能力由后端内部接口统一代理，Agent 不持有 `SERPAPI_API_KEY`。

### 运行模式
Agent 运行模式仅支持 `auto`、`executor` 和 `host`。
- `auto` 只有在 executor 地址与非空 token 同时可用时才选择 executor，否则使用 host。
- 显式 `executor` 缺少任一配置会失败关闭，不会回退到 host。
- 旧的 Docker runtime、容器名和容器运行时字段不再受支持；本地回滚请明确设置 `MEMEMEOW_AGENT_RUNTIME_MODE=host`。
- 人工诊断仍可在当前 Compose project 中运行 `docker compose exec mememeow-agent-runtime ...`，不需要也不应配置真实容器名。

## 3. 本地开发与服务运维

### 前后端联合开发
前端开发时可保留 Compose 后端运行，并单独启动带热更新的 Vite 开发服务器：

```bash
./start.sh --vite
```
开发服务器默认访问 `http://127.0.0.1:5275`，`/api` 和 `/media` 请求会代理到 `http://127.0.0.1:8275`。该命令在前台运行，按 `Ctrl-C` 停止；可通过 `MEMEMEOW_VITE_HOST` 和 `MEMEMEOW_VITE_PORT` 覆盖监听地址与端口。宿主机单独运行测试时使用 `uv sync --dev`；应用运行不需要宿主机 Node.js、npm、Python 虚拟环境或 tmux，前端构建在 API 镜像中完成。

### 安装共享 Agent skill（仅宿主机开发工具需要）
```bash
./scripts/install-agent-skills.sh
```
该脚本只创建指向 `skills/research-meme-context` 的相对符号链接，不会覆盖真实目录。容器镜像内固定使用 `/skills/research-meme-context` 和 `/opt/mememeow/node_modules`。

### 运维指令
- 单独维护 Agent executor：`./scripts/agent-runtime.sh build|start|check|stop|restart|logs`（`check` 包含全面的安全边界验证）。
- 单独管理数据库：`./database.sh start|stop|restart|status|logs|migrate`。
- 本地 Adminer 查看器：`./db-viewer.sh start`，默认挂载在 `http://127.0.0.1:8080`（支持通过 `MEMEMEOW_DB_VIEWER_PORT` 和 `MEMEMEOW_DB_VIEWER_IMAGE` 等环境变量自定义）。

## 4. 视觉服务 (Visual Service) 进阶

视觉服务只暴露 Compose 内部 `8276` 端口，使用 `MEMEMEOW_VISUAL_WEIGHTS_DIR`、`MEMEMEOW_VISUAL_WEIGHTS_SHA256` 和 CPU 线程变量配置。
- 镜像内固定包含 DINOv2 官方源码提交 `7764ea0f912e53c92e82eb78a2a1631e92725fc8`。
- 视觉服务所在网络只与 Compose `mememeow` 主后端共享，Agent 回调使用 `http://mememeow:8275`，视觉状态使用 `http://mememeow-visual:8276/health`。
- 权重缺失时容器仍可健康启动，但视觉任务会返回 `visual_model_not_configured`，不会使用随机模型伪造结果。完整的模型许可、资源权衡和基线记录见 `docs/visual-model-baseline.md`。

## 5. 回滚与排障最佳实践

- **安全停机**：停机或诊断优先使用 `./start.sh stop|status|logs`；**不要使用 `docker compose down -v`**，否则会删除持久数据。
- **数据保留**：named volume、图片和 PostgreSQL 数据在 `stop` 时不会被删除。从旧 `data/opencode` 迁移到 `mememeow-agent-runtime-data` 前先停止 executor 并保留备份。
- **排障线索**：任务结果文件按保留策略保存，便于排查 `agent_result_file_missing`、`agent_result_file_invalid_json` 等错误。
