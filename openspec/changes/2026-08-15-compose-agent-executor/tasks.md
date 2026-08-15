## 1. OpenSpec 与协议

- [x] 1.1 创建本 change 的 proposal、design、spec delta 和任务清单，描述 executor 替代旧 docker exec 的边界。
- [x] 1.2 定义带 Bearer token 的健康、提交、状态和取消 HTTP 契约，拒绝未知字段和任意 shell 能力。

## 2. Executor 实现

- [x] 2.1 新增非 root 容器内 executor，固定 OpenCode 参数、环境白名单、图片路径、结果大小和任务目录。
- [x] 2.2 实现同步等待、异步状态、超时、取消、进程组终止、并发池和队列背压。
- [x] 2.3 实现健康探针，验证 token、OpenCode、runtime 可写、图片/Skill 只读和 Docker socket 缺失。
- [x] 2.4 保持任务结果文件原子交付，executor 基本检查与后端完整 schema/业务校验分层执行。

## 3. API 与配置

- [x] 3.1 新增 executor HTTP 客户端和配置字段，Compose 模式不调用 Docker CLI 或 Docker socket。
- [x] 3.2 将 OpenCodeRunner 生产路径改为 executor，保留宿主模式和历史测试夹具兼容。
- [x] 3.3 使用 Compose 内部视觉健康 URL，修复 API 对宿主相对权重路径的错误可用性判断。
- [x] 3.4 补充稳定错误码、认证失败、超时/取消和 runtime 不可用映射。

## 4. Compose 与镜像

- [x] 4.1 Agent 镜像以 executor HTTP 服务为入口，不安装或挂载 Docker socket。
- [x] 4.2 Agent/API 使用 named runtime volume，executor/visual 只 expose 内网端口，API 绑定 `127.0.0.1:8275`。
- [x] 4.3 配置 executor 健康依赖、restart 策略、Compose DNS 回调和 named volume 中的非 root 随机 token 引导。
- [x] 4.4 更新 `.env.example` 与运行文档，说明 token、端口、volume 和旧 runtime 迁移取舍。

## 5. 验证

- [x] 5.1 添加 executor HTTP 集成测试，覆盖认证、非法字段/路径、结果传递和取消。
- [x] 5.2 运行后端全量测试、Python 编译、`git diff --check` 和 OpenSpec strict validation。
- [ ] 5.3 在可用 Docker daemon、模型凭据和视觉权重条件下运行完整 Compose 真实 e2e；当前需在发布环境复核。
