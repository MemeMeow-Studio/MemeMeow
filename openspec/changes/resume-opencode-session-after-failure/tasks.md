## 1. 任务与数据契约

- [x] 1.1 为任务 attempt 和 Agent session 设计可选持久字段或诊断摘要，补充数据库迁移、ORM 映射、序列化和旧任务兼容默认值。
- [x] 1.2 定义可续跑错误分类、不可续跑原因、attempt 历史脱敏结构和 `resume_available` 任务状态响应字段。
- [x] 1.3 扩展任务状态 API 测试，覆盖首次错误、续跑次数、session 关联、不可续跑和跨 scope 拒绝。

## 2. Executor 与 OpenCode 会话采集

- [x] 2.1 修改 executor 进程输出处理，在成功和非零退出路径都尽力提取 session ID，并为每次进程提交生成独立 executor attempt ID。
- [x] 2.2 增加按明确 session ID 查询和启动续跑的受控内部协议，拒绝全局最近会话和不匹配任务事实。
- [x] 2.3 保证续跑不会清理已有 draft 或有效中间产物，且结果文件仍经过路径、大小、JSON 和 schema 校验。
- [x] 2.4 为 executor 增加重复终态 ID、续跑成功、session 不匹配、旧 attempt 晚到和进程失败采集测试。

## 3. Worker 恢复编排

- [x] 3.1 在 OpenCode Runner 中持久化非零退出时可验证的 session，并把 provider 网关、网络、进程失败与结果/输入/计量错误分类。
- [x] 3.2 为允许恢复的错误实现有界退避、单任务续跑次数和总超时控制；续跑使用新的 executor attempt 而保留原业务 Task。
- [x] 3.3 在 PostgreSQL Worker 中接入 session 续跑决策、claim generation fencing、grant 复用和 `unknown_execution` 禁止重放语义。
- [x] 3.4 将首次错误与后续调度错误追加保存，禁止 `task_exists` 或其它恢复错误覆盖原始 provider 诊断。

## 4. 端到端验证与部署

- [x] 4.1 增加模型网关 429/5xx、连接中断、session 缺失、外部执行未知和目标 SHA 变化的恢复矩阵测试。
- [x] 4.2 增加重启恢复测试，验证可续跑 session 继续同一逻辑任务、不可续跑任务进入稳定终态、旧 Worker 结果不会写回。
- [x] 4.3 更新配置示例、健康/任务详情文档和运维说明，明确默认关闭自动续跑、退避和总次数上限。
- [x] 4.4 执行 OpenSpec 校验及相关 Python、executor、API 和 PostgreSQL 集成测试，记录回滚开关验证结果。
