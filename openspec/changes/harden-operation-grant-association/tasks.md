## 1. 请求事实与 association 契约

- [x] 1.1 为 `OperationRequest` 增加服务端 `input_digest` 和稳定请求指纹，保持 operation vocabulary、错误码和客户端字段过滤不变。
- [x] 1.2 为 `GrantAssociation`、内存 store 和 gateway 命中路径增加完整事实比较；冲突、非法状态和无法安全判断统一 fail-closed。
- [x] 1.3 将 `acquired` 作为唯一可执行状态，terminal/unknown 只允许恢复观察，禁止重新 acquire、自动重放和状态回退。
- [x] 1.4 支持 pipeline 后置 Task 绑定并同步 association 请求指纹，拒绝改绑和跨 scope 事实。

## 2. PostgreSQL 事实持久化

- [x] 2.1 为 operation grant ORM 增加 source、units 和 request fingerprint 字段、约束与兼容旧行的可空迁移边界。
- [x] 2.2 新增前向 Alembic migration 并更新 expected schema revision；历史缺失事实不回填猜测值。
- [x] 2.3 让 PostgreSQL get/put/acquire/transition/bind_task 共用 scope-safe 事实校验和 terminal 门禁。

## 3. 受影响路径

- [x] 3.1 更新普通上传、合集导入和删除 API 使用 store.acquire，并传递服务端图片摘要。
- [x] 3.2 更新 pipeline/standalone Agent、任务提交、执行器 commit 和 Worker 恢复使用同一完整事实。
- [x] 3.3 更新反向图片 provider 的 cache miss grant 命中和 unknown/terminal 恢复分支，禁止重新联系 provider。

## 4. 测试与验证

- [x] 4.1 增加内存 store 相同事实复用、五类事实冲突、pipeline 绑定和 terminal/unknown 不可执行单元测试。
- [x] 4.2 增加 PostgreSQL 持久字段、事实冲突、terminal acquire、旧行 fail-closed 和并发幂等测试。
- [x] 4.3 运行 API、上传/删除、Agent、Worker、反向图片相关回归测试；无 PostgreSQL/provider 环境时记录跳过项。
- [x] 4.4 运行严格 OpenSpec 校验、compileall、格式和 `git diff --check`，确认公共代码不引入账户或额度逻辑。
