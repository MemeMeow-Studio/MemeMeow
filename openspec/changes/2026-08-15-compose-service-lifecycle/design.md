## 背景

Agent executor、视觉服务和 API 已经具备 Compose 内部网络边界，但旧启动脚本仍假设
宿主机有 tmux、npm 和 Python 虚拟环境。迁移的核心不是再增加一个包装命令，而是让
Compose 成为唯一的进程、依赖和健康状态来源。

## 设计目标

- 一条命令启动完整应用，重复执行不删除数据且不产生第二套宿主进程。
- 在启动前发现 Docker 权限、Compose 插值、数据库迁移和依赖健康问题。
- 失败时保留容器现场并给出可执行的 `ps` 与日志诊断。
- 仅把 API 的回环端口发布到宿主机，内部服务通过 Compose DNS 通信。

## 关键决策

### 1. 启动脚本只编排 Compose

`start.sh start` 先验证 `docker compose config --quiet`，清理旧 tmux 会话和迁移前的
`8275`、`5275`、`8000`、`5173` 宿主进程，再执行
`docker compose --profile app up -d --build`。如果当前 `mememeow` 服务已经运行，
脚本不会杀 Docker 代理；Compose 自身负责复用或重建容器。

`stop` 使用 Compose `stop`，随后清理遗留宿主端口；不执行 `down`，因此不会删除容器
定义之外的 named volume。`status` 只读 `ps --all`，`logs` 默认显示有限尾部并透传
服务筛选和跟随参数。

### 2. Docker 组权限

脚本先尝试当前进程的 Docker daemon 权限。若当前 shell 尚未刷新 `docker` 组，则对每次
Compose 调用通过安全转义后的命令字符串运行 `sg docker`；若 Docker CLI、daemon 或
docker 组不可用，提前返回明确错误，不运行旧宿主服务。

### 3. 健康门禁与诊断

Compose 依赖顺序为 PostgreSQL healthy、`db-init` 成功、Agent executor healthy、视觉
服务 healthy，最后启动 API。API healthcheck 只判断 HTTP `/health` 能返回 `ok` 或
`degraded`，因为缺少可选视觉权重不应阻止 API 提供检索界面。脚本额外轮询 Compose
状态和回环 `/health`，随后验证 `/`、`/health`、`/config` 的 HTTP 状态。依赖退出或超时
时输出 `ps --all` 与有限日志，不自动删除现场。

### 4. 网络与持久化边界

Compose 只发布 `127.0.0.1:8275:8275`。Agent `8277` 和视觉 `8276` 仅使用 `expose`
和内部网络；API、Agent 都没有 Docker socket 挂载。PostgreSQL、图片 bind mount、
Agent runtime named volume 和 executor secret named volume 继续沿用既有名称和内容，
停止操作不触碰它们。

## 迁移与回滚

迁移时先运行 `./start.sh stop` 收束旧 Compose/宿主实例，再运行 `./start.sh start`。
旧 tmux 会话和固定端口会在启动前再次清理。回滚或诊断只需 `./start.sh stop` 并保留
named volume；禁止使用 `docker compose down -v`，旧 runtime 迁移前保留宿主备份。
