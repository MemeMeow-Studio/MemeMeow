## 1. 持久化提交来源与迁移

- [x] 1.1 为三类图片阶段 Task 增加受约束的 `pipeline`/`standalone` 提交来源、阶段和可选 Job 关联持久化事实及查询索引。
- [x] 1.2 回填能够由图片 Job 阶段关系可靠证明的历史 `pipeline` Task，并将无法可靠关联的历史图片 Task 标记为未归类且禁止全部重试。
- [x] 1.3 更新活动任务去重键和数据库约束，使 scope、图片 SHA、阶段、提交来源、配置及 Agent 策略都参与去重，并阻止一条 Task 同时归属 Job 与独立提交。

## 2. 安全提交与重试 API

- [x] 2.1 保持上传、单图处理、批量处理和完整重试走完整图片处理 Job 服务，并明确返回 Job 提交模式和标识。
- [x] 2.2 新增受限的单图独立阶段提交 API，只接受允许的阶段和规范化 Agent 业务策略，并从解析后的 scope 与当前 Meme 派生全部可信输入。
- [x] 2.3 收紧通用 `POST /tasks/{task_id}/retry`，拒绝所有图片阶段 Task 的直接重试，提供稳定错误并确保不创建 Task、Job revision 或 grant。
- [x] 2.4 扩展 Job/Task 查询与列表 DTO，公开安全的提交来源、阶段和可选 Job 引用，并保持跨 scope 查询不泄露资源存在性。

## 3. 图片 Worker 与阶段结果

- [x] 3.1 更新 `ImageProcessingWorker`，按持久化提交来源认领和执行三类图片阶段 Task，并维持既有 claim、lease、fencing、scope、SHA 和配置校验。
- [x] 3.2 限制父 Job reconcile 和下游阶段创建只发生在 `pipeline` Task；验证独立视觉、Agent 和文本 Task 均不会调度其他阶段或创建父 Job。
- [x] 3.3 让独立 Agent Task 复用 operation policy、grant、attempt 和 callback fencing，并在语境变化后仅使过期文本向量不可检索而不自动生成文本 Task。
- [x] 3.4 保证独立与 Job 所属任务并发写同一阶段时，只有仍匹配当前图片版本和输入签名的结果可被采纳。

## 4. 工作台操作与层级展示

- [x] 4.1 更新前端 API 客户端与状态轮询，分别处理完整 Job 响应和独立阶段 Task 响应。
- [x] 4.2 在图片库提供“完整重试”及视觉、Agent、文本 embedding 的“仅重试”操作，并呈现活动去重、policy 拒绝和目标变化诊断。
- [x] 4.3 在处理任务工作区将完整 Job 渲染为可展开的三阶段父项，将独立图片阶段 Task 渲染为独立工作项，不依赖前端推测归属。

## 5. 测试、迁移验证与安全回归

- [x] 5.1 为持久化迁移、来源约束、模式隔离去重、终态独立 Task 新建及历史图片 Task 的全部重试拒绝添加 PostgreSQL 测试。
- [x] 5.2 为完整 Job 与三个独立阶段分别测试：阶段调度边界、失败停止、结果有效性、文本向量失效及不创建下游 Task。
- [x] 5.3 覆盖跨 scope 标识、伪造 payload、过期 claim、并发 Job/独立 Task、Agent policy 拒绝、grant 使用，以及未认证、跨 Task、跨 scope/SHA 和重放 callback 的安全回归。
- [x] 5.4 添加前端单元与端到端测试，验证 Job 阶段层级、独立任务展示、两种重试操作及错误反馈。
- [x] 5.5 运行数据库迁移演练、后端测试、前端测试、OpenSpec strict 验证和桌面/移动端截图检查。
