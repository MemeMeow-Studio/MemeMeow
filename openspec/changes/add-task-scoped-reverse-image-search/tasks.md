## 1. 数据模型与持久化

- [x] 1.1 新增 `reverse_image_usage_events` SQLAlchemy 模型，包含 scope、任务、图片、幂等请求标识、缓存状态、供应商调用状态、结果与时间字段，并补充中文模块/类/字段职责说明。
- [x] 1.2 创建只向前 Alembic 迁移，建立 scope、任务和图片复合外键、`request_id` 唯一约束、非空与枚举检查约束，以及按 scope/任务统计所需索引。
- [x] 1.3 为数据库环境增加创建、幂等读取/更新和按任务聚合 usage event 的仓储操作，确保不同 scope 无法相互引用或统计。
- [x] 1.4 增加数据库模型、迁移契约和多 scope 隔离测试，覆盖重复 `request_id`、失败事件和供应商调用计数。

## 2. 后端反向图片服务

- [x] 2.1 从现有 Skill 脚本提取供应商无关的请求模型、缓存 identity、快照读取、TTL、原子写入、同键文件锁和脱敏逻辑到后端模块，并保持已有缓存 schema 可读。
- [x] 2.2 实现 SerpApi Google Lens 后端适配器，统一处理图片上传、Lens 查询、超时、非法响应、空结果和敏感字段清理，确保一次逻辑检索只产生一次调用事件。
- [x] 2.3 实现反向图片服务编排：校验任务状态与 payload 策略、在锁内二次检查缓存、持久化 usage event、调用供应商、更新事件终态并返回供应商无关 JSON。
- [x] 2.4 增加缓存命中、空结果过期、刷新、供应商失败、并发同键请求、进程中断状态和缓存兼容的离线单元测试。

## 3. 内部接口与运行边界

- [x] 3.1 新增无认证的 `POST /internal/reverse-image/search` multipart 接口，校验 `task_id`、图片格式/大小和检索参数，并映射 `reverse_image_forbidden`、无效任务、不可用和可重试供应商错误。
- [x] 3.2 让接口只从服务端任务记录读取 scope 与 `reverse_image_policy`，拒绝不存在、非语境生成或非运行中的任务，并忽略调用方任何自报策略。
- [x] 3.3 调整 Docker Compose 和运行时地址配置，使共享 Agent 容器能访问内部接口但不向 Agent 注入 `SERPAPI_API_KEY`，同时让后端继续独占读取供应商密钥。
- [x] 3.4 增加内部接口集成测试，覆盖 `forbid`、`auto`、历史缺省、非运行任务、未知任务、缓存命中和未命中计数，并断言响应不泄露密钥或临时凭据。

## 4. Agent Runner 与 Skill 薄客户端

- [x] 4.1 修改 Runner，为 Agent 传递内部接口地址和当前 `task_id`，在 prompt 中明确本任务策略，并在 Host 与 Docker 两种模式都移除 SerpApi 密钥注入。
- [x] 4.2 将 `serpapi_google_lens.py` 改为薄 CLI 客户端：保留现有图片和检索参数，调用内部 multipart 接口，输出统一 JSON，并以稳定消息报告接口错误。
- [x] 4.3 更新 `research-meme-context` Skill 与参考文档，只允许 Agent 通过薄客户端使用项目反向图片能力，删除 Agent 可见的供应商直连命令和本地密钥加载说明。
- [x] 4.4 更新 Agent 镜像、runtime probe 和 Runner/Skill 测试，验证脚本可访问接口、只读图片可上传、Agent 环境不含供应商密钥且 `forbid` prompt 不允许调用。

## 5. 任务策略与审计汇总

- [x] 5.1 在上传、单图和批量请求模型中加入严格的 `reverse_image_policy=forbid|auto`，缺省为 `forbid`，并在提交 `auto` 时校验供应商可用状态。
- [x] 5.2 将规范化策略写入每个语境任务 payload，并让历史缺失字段在 Worker 侧按 `forbid` 读取，不从环境或当前设置推断。
- [x] 5.3 修改活动任务去重：同图同内容同策略复用；同图同内容不同策略返回 `generation_policy_conflict`；保持单图片活动任务互斥。
- [x] 5.4 在语境任务成功和失败终态按 usage events 汇总 `reverse_image` 摘要并写入 Task.result；成功写回语境时同步写入 Meme provenance，拒绝 Agent 自报值覆盖后端统计。
- [x] 5.5 在脱敏配置状态中增加反向图片服务是否可用的布尔字段，并在任务详情响应中安全返回策略和审计摘要。
- [x] 5.6 增加 API 与任务服务测试，覆盖上传、单图、批量、重试、默认禁止、无供应商的自动策略、策略冲突、缓存/供应商计数及 Agent 失败后的审计保留。

## 6. 前端策略与可观察性

- [x] 6.1 新增 `forbid`/`auto` 两段式策略控件和共享前端状态，默认禁止，并明确提示自动模式可能把图片发送给第三方反向图片服务。
- [x] 6.2 将策略控件接入上传自动生成、图片库批量生成和失败任务重试，确保 API 客户端分别序列化 multipart 与 JSON 字段。
- [x] 6.3 根据后端可用状态禁用或标记 `auto`，并为 `reverse_image_unavailable`、`reverse_image_forbidden` 和 `generation_policy_conflict` 提供现有错误展示路径可读的消息。
- [x] 6.4 在任务详情展示本次策略、是否使用反向图片、缓存命中数和实际供应商调用数，不展示供应商密钥、临时标识或内部缓存路径。
- [x] 6.5 增加 Vue 单元测试和 Playwright 流程测试，覆盖三个可见入口、默认禁止、自动不可用、请求序列化、重试切换策略及任务审计展示，并检查移动端控件不溢出或遮挡。

## 7. 回归、文档与验收

- [x] 7.1 更新 API、部署和运行说明，记录两档语义、内部接口信任边界、实际调用计数口径、Agent 不持有供应商密钥及未来认证扩展点。
- [x] 7.2 使用项目 Python 虚拟环境运行后端单元与集成测试，运行前端单元、构建和 E2E 测试，并修复所有相关回归。
- [ ] 7.3 在共享 Agent 容器执行端到端验收：分别验证 `forbid` 拒绝、`auto` 缓存命中、`auto` 未命中和供应商失败，核对 usage event、Task.result 与 Meme provenance 一致。（当前会话无权访问 Docker daemon，未执行）
- [x] 7.4 检查 Agent 进程环境、日志、缓存、接口响应和任务结果，确认不存在 `SERPAPI_API_KEY`、`image_id` 或供应商私有归档 URL。
- [x] 7.5 对策略绕过、跨 scope 引用、并发重复计数、任务终态竞态、历史 payload 和服务回滚进行多 Agent 对抗性审查，并处理确认的问题。
