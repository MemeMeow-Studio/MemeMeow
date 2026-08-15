## Context

应用的研究任务需要 OpenCode、图片只读挂载、Skill、网络和持久 session，但业务
API 不应拥有 Docker 编排权限。此前的共享容器设计由后端执行 `docker exec`，其
健康检查只能证明容器中工具存在，不能表达任务状态、取消或并发背压。Compose
还把宿主 `data/opencode` bind mount 到非 root 容器，首次启动时目录 owner 可能
不匹配。

## Goals / Non-Goals

**Goals:**

- 让 API 与 Agent 之间只有固定、带 token 的内部 HTTP 协议。
- 让 executor 独立管理 OpenCode 子进程，支持同步等待、状态查询、超时、取消、并发上限和队列背压。
- 保留 `/runtime/task-results/<task_id>/result.json.tmp` 结果文件协议，后端执行完整 schema 和业务校验。
- 让 Compose 健康依赖、端口绑定、内部 DNS、runtime 权限和视觉服务健康状态可自动验证。

**Non-Goals:**

- 不为 Agent 提供任意 shell、任意命令、任意环境变量或数据库 API。
- 不尝试限制 OpenCode 在 Agent 容器中的开放网络目的地；反向图片能力仍由现有后端任务策略控制。
- 不改变 PostgreSQL 任务模型、前端任务接口或 OpenCode 研究输出 schema。

## Decisions

### 1. 容器内 executor 而不是 API 侧 Docker exec

Agent 镜像入口改为 `python3 -m executor.server`。服务使用 Python 标准库
`ThreadingHTTPServer`，减少 Agent 镜像依赖，且同一容器内可直接启动固定 OpenCode
命令。生产 Compose 使用独立 named volume 持久化 0600 的随机 token：非 root
executor 首次启动原子创建 `/run/agent-executor-secret/token`，API 以只读方式读取
同一路径。API 仍只持有 `MEMEMEOW_AGENT_EXECUTOR_URL` 和内存中的非空 token；生产
地址固定为 `http://mememeow-agent-runtime:8277`。

任务请求只允许 `task_id`、`image_relative_path`、`reverse_image_policy`、
`timeout_seconds` 和 `wait`。executor 拒绝未知字段、绝对路径、`..`、符号链接、
任意 prompt/command/env/workdir，并从自身环境白名单组装 OpenCode 环境。模型
连接配置由 Compose 显式注入 executor，不读取 `.env` 文件。

### 2. 状态、取消和结果

`POST /v1/tasks` 默认同步等待，也可设置 `wait=false`；`GET /v1/tasks/{id}`
读取受限状态；`POST /v1/tasks/{id}/cancel` 只终止该任务的进程组。executor
以固定并发池处理任务，队列超过背压上限返回 429。每项任务只在自己的 runtime
结果目录写草稿并原子 rename；executor 做大小和基本 JSON 检查，API 再做 JSON
Schema、Pydantic、图片指纹和数据库写回校验。

后端请求超时后调用取消接口并返回 `agent_timeout`/`task_interrupted`；executor
崩溃由 Compose `restart: unless-stopped` 重启，任务状态由数据库任务服务收束。

### 3. Compose 网络和持久化

executor 仅 `expose: 8277`，视觉服务仅 `expose: 8276`；只有 API 发布
`127.0.0.1:8275`。Agent 和 API 通过 Compose 默认网络使用 `mememeow` 与
`mememeow-agent-runtime` DNS。Agent 回调后端使用 `http://mememeow:8275`，
不使用 loopback、`host.docker.internal` 或 host-gateway。

`mememeow-agent-runtime-data` named volume 同时挂载为 Agent 的 `/runtime` 和
API 的 `/app/data/opencode`。named volume 从镜像 `/runtime` 的 owner 初始化，
避免 bind mount 覆盖非 root 权限；该 volume 保留 OpenCode DB、缓存、日志和结果。

### 4. 视觉状态来源

Compose 为 API 注入 `http://mememeow-visual:8276/health`。`VisualInferenceClient`
读取该内部健康响应判断模型可用性和模型身份，不根据 API 容器内不可见的宿主
权重相对路径误报。视觉服务缺权重时 API 可以健康启动，但视觉任务返回既有稳定
配置错误；有权重时 embedding 请求仍只走内部 URL。

## Risks / Trade-offs

- named volume 不会自动把旧宿主 `data/opencode` 内容迁移进去；部署迁移需按文档手动复制或保留旧 volume。这样换取新 checkout 非 root 启动的确定性。
- executor token 是单一服务间共享凭据，泄露会允许内部任务提交；token 只在独立 named volume 中以 0600 保存，不写日志、不进入 Agent 子进程环境。删除 secret volume 会触发下一次启动生成新 token，API 依赖 executor 健康后再启动。更细粒度的任务签名留待多租户需求。
- executor 任务状态在其进程内存中，executor 重启后进行中的 OpenCode 被终止；数据库任务租约负责重新认领/收束，结果文件保持由后端校验。
- OpenCode 仍可以使用容器网络和自身模型凭据，这是产品研究能力所需的权限；本 change 不承诺出网目的地隔离。

## Migration Plan

1. 停掉旧 Compose 应用服务和宿主 tmux 后端；全新部署无需向宿主 shell 或 `.env` 写入 executor token。
2. 构建 Agent 镜像并启动 executor；其在 secret named volume 中原子生成随机 token，等待真实 `/health` token 探针通过。
3. 启动 PostgreSQL、db-init、视觉服务和 `--profile app` API，确认 API 仅监听回环端口。
4. 用 stub OpenCode/测试图片验证一次成功任务、非法路径拒绝、取消和结果文件写回；在模型凭据和视觉权重可用时执行真实任务。
5. 旧宿主 runtime 迁移完成后再清理旧 bind mount；回滚时停止 API/executor，保留 named volume 和旧 runtime 以便诊断。
