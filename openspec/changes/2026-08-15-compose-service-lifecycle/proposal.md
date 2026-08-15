## 为什么

此前 `start.sh` 仍在宿主机通过 tmux 启动 API 和 Vue 开发服务器，与已经完成的
Compose Agent executor、视觉服务和 PostgreSQL 架构不一致。这样会让 API 使用错误的
内部地址、启动依赖和数据边界，也无法把实际运行态作为一套可诊断的全栈服务管理。

## 变更内容

- 将 `start.sh` 改为唯一的 Compose 全栈生命周期入口，提供 `start`、`stop`、`status`
  和 `logs` 命令，不再启动或附着 tmux。
- 启动前清理旧 tmux 会话和固定遗留宿主端口；Compose API 保持幂等，不误杀其 Docker
  代理端口。
- 启动时校验 Compose 配置，等待数据库迁移、Agent executor、视觉服务和 API 健康，
  失败时输出有限状态与日志诊断。
- 为 API 增加 Compose healthcheck，并让 API 等待视觉服务健康；保持 API 只绑定
  `127.0.0.1:8275`，Agent 与视觉端口只存在于 Compose 网络。
- 更新快速开始与配置说明，保留 PostgreSQL、图片、Agent runtime 和 executor token
  named volume，不提供删除 volume 的快捷路径。

## 非目标

- 不改变 Agent executor 协议、任务业务语义或视觉模型身份。
- 不把数据库、Agent executor、视觉服务或 Docker socket 暴露到宿主机。
- 不用 `docker compose down -v` 自动清理数据，也不引入宿主机 Node.js/Python 运行时。

## 影响

运行入口、Compose 编排、API 容器探活、README、`.env.example` 和 OpenSpec 文档发生变化。
已有 `database.sh`、`db-viewer.sh` 和 Agent 运维脚本仍可独立管理各自职责，但应用启动、
停止、状态与日志统一由 `start.sh` 负责。
