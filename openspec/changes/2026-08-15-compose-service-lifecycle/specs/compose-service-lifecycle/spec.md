## 目的

定义 MemeMeow 从宿主 tmux 迁移到 Docker Compose 后的唯一启动、停止、状态、日志和健康
边界，保证运行态与既有容器架构一致。

## ADDED Requirements

### Requirement: 应用生命周期必须由 Compose 管理

`start.sh` 的 `start` MUST 通过 Compose 启动 PostgreSQL、数据库迁移、Agent executor、
视觉服务和 API 全栈，不得启动宿主 Python、Node、Vue 或 tmux。`stop`、`status`、`logs`
必须分别停止、读取状态和读取 Compose 日志，且默认不得删除 named volume。

#### Scenario: 首次或重复启动

- **当** 用户运行 `./start.sh start`
- **那么** 脚本校验 Compose 配置、清理旧 tmux/遗留固定端口，并幂等执行
  `docker compose --profile app up -d --build`
- **并且** API、Agent、视觉和数据库均由 Compose 容器提供

#### Scenario: 停止服务

- **当** 用户运行 `./start.sh stop`
- **那么** Compose 应用服务停止，遗留 tmux 和固定宿主端口进程也被收束
- **并且** PostgreSQL、图片、Agent runtime 和 executor token named volume 不被删除

### Requirement: 启动必须等待健康状态并提供失败诊断

启动 MUST 等待 PostgreSQL、成功的数据库迁移、Agent executor、视觉服务和 API HTTP 健康。
依赖失败或超时必须返回非零退出码，并输出 `docker compose ps --all` 和有限尾部日志，
不得静默启动宿主回退进程。

#### Scenario: 依赖未就绪

- **当** 数据库迁移失败、executor/视觉 healthcheck 失败或 API 在超时内不可访问
- **那么** `start.sh` 返回失败并保留容器现场用于 `status`/`logs` 诊断

#### Scenario: API 探活

- **当** 全栈达到健康门禁
- **那么** 脚本验证回环 `http://127.0.0.1:8275/`、`/health` 和 `/config`

### Requirement: 网络发布和持久化边界必须保持受限

Compose MUST 仅发布 `127.0.0.1:8275:8275`。Agent executor 和视觉端口只能通过 Compose
内部网络访问；API 与 Agent 容器不得挂载 Docker socket。停止、重启或重复启动不得删除
数据库、图片、runtime 和 executor secret named volume。

#### Scenario: 宿主端口检查

- **当** 用户查看 `docker compose ps`
- **那么** API 仅显示回环 `8275` 发布，Agent/视觉没有宿主 published port

#### Scenario: Docker 权限未刷新

- **当** 当前 shell 尚未获得 Docker 组权限但 `docker` 组存在
- **那么** 生命周期命令通过安全转义的 `sg docker` 调用 Compose，否则提前给出诊断错误
