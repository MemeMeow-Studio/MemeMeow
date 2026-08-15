## 1. OpenSpec 与边界

- [x] 1.1 创建 Compose 生命周期变更提案、设计、规格和任务清单。
- [x] 1.2 明确 tmux 清理、固定端口、Docker 组权限、健康门禁和 named volume 不删除约束。

## 2. 启动与编排实现

- [x] 2.1 将 `start.sh` 改为 `start|stop|status|logs` Compose 入口，移除宿主 Python、npm 和 tmux 启动路径。
- [x] 2.2 实现 Compose 配置预检、幂等启动、失败诊断和 Docker 组权限回退。
- [x] 2.3 为 API 增加真实 HTTP healthcheck，并让 API 等待视觉服务健康。
- [x] 2.4 保持 API 回环发布、Agent/视觉内网端口、Docker socket 缺失和全部持久 volume 边界。

## 3. 文档与验证

- [x] 3.1 更新快速开始、运维命令、迁移说明和 `.env.example` 的 Compose 地址提示。
- [x] 3.2 运行 shell/Compose 配置检查、后端测试、OpenSpec strict validation 和 `git diff --check`。
- [x] 3.3 将实际运行态从旧入口切换到 Compose，并验证 `/`、`/health`、`/config` 与 `docker compose ps`。
