> **归档说明**：本 change 已被 `introduce-postgres-scoped-persistence` superseded-by。新 change 归档后，本 change 仅作为历史实现记录使用 `--skip-specs` 归档。

## 1. OpenCode 运行时与共享 skill

- [x] 1.1 将 `research-meme-context` 移到受版本控制的 `skills/`，新增幂等安装脚本，为 `.agents/skills` 和 `.opencode/skills` 创建可移植相对链接，并更新安装文档与忽略规则。
- [x] 1.2 新增 OpenCode 可执行文件、服务地址、密钥、模型、固定 runtime、超时和共享依赖路径配置；在 workspace 生成无密钥通用 `opencode.json`，只注册 `gpt-5.6-luna`，配置查询保持脱敏，缺失项返回稳定错误。
- [x] 1.3 实现固定 workspace 初始化，链接同一份 skill 与预安装 `node_modules`，设置固定 `OPENCODE_DB`，验证任务路径中不会调用包管理器或创建逐任务依赖目录。
- [x] 1.4 实现语境生成进程的单 worker 与跨进程 `worker.lock`，按创建时间和 task ID 稳定消费队列，并为 runtime 初始化、互斥和缺失依赖补充测试。
- [x] 1.5 新增会话检查启动脚本，复用固定 OpenCode runtime 与 DB，隔离父目录配置，支持 TUI `/sessions` 和非交互列表，并补充验证与使用文档。

## 2. 统一持久任务服务

- [x] 2.1 用可序列化 payload 和 task-type handler 注册表替换内存 `TaskManager` 的 Python closure 提交方式，覆盖 `cache_generation`、`metadata_repair` 和 `meme_context_generation`。
- [x] 2.2 定义持久任务模型和稳定错误，包含状态、时间、尝试次数、结果与有限诊断；为语境任务补充图片相对路径/SHA-256、模型、skill 哈希、session ID 和 sidecar 哈希。
- [x] 2.3 实现逐任务 JSON 的临时文件、fsync、原子替换、读取校验和损坏记录隔离；用任务类型和规范化 payload 去重活动任务。
- [x] 2.4 实现启动恢复与关闭收束：重新排队持久 `queued` 任务，将遗留 `running` 任务标记为 `task_interrupted`，终止仍受管理的子进程组并保留终态记录。
- [x] 2.5 保持 `GET /tasks/{task_id}` 的响应契约，新增按状态、类型和 cursor 分页的 `GET /tasks` 受限摘要列表；迁移缓存生成和 metadata repair 的提交路径。
- [x] 2.6 为三种任务的提交、进度、结果、失败、重启恢复、并发读取、稳定排序、筛选、分页与敏感字段排除补充任务 API 测试。

## 3. OpenCode runner 与结果解析

- [x] 3.1 以无 shell 的参数数组启动 `opencode run --format json --file`，每张图片创建独立 session，流式写入临时文件而不设置 stdout/stderr 总字节门禁，并在超时或取消时终止整个进程组。
- [x] 3.2 解析 JSONL 事件并记录 session ID/阶段，正常退出后通过公开 loopback session messages API 读取完成 session，只选择最后一条完整 assistant 消息的 text parts。
- [x] 3.3 实现严格候选提取：优先解析完整原始 JSON，只兼容唯一 JSON fenced block，拒绝多个对象、额外说明、工具结果和花括号猜测。
- [x] 3.4 引入并配置 JSON Schema 校验与 URI format 检查，再使用 `MemeContext` 完成字段清理、数量和长度校验，输出稳定的解析/schema 错误。
- [x] 3.5 在写回前复核目标路径、文件存在性和图片 SHA-256；通过研究来源与人工字段保护原子提交 sidecar，保存结果哈希并使检索缓存失效。
- [x] 3.6 使用录制的 OpenCode event/session-message 夹具覆盖分块文本、大输出、工具输出、无 session、非零退出、超时、无效 JSON、多个 fenced block、schema 失败、目标变化和成功写回。

## 4. 上传流程与 VLM 移除

- [x] 4.1 调整上传流程，使图片和 `pending` sidecar 成功后自动创建语境生成任务，并在逐文件结果中返回 `metadata_job_id`；任务记录失败不回滚有效图片。
- [x] 4.2 保留显式 `auto_name=true`，把它写入任务 payload；Agent 成功写入非空 title 后才安全异步重命名图片与 sidecar，冲突或目标变化只报告命名失败。
- [x] 4.3 新增单图语境生成/重试与既有图片批量补齐入口，对缺失、`pending`、`partial` 和 `repair_required` 记录先执行必要 repair，再按图返回创建、复用和失败结果。
- [x] 4.4 删除 VLM 配置、`backend/labeling.py`、单图描述与批量 VLM 标注接口、对应前端流程和测试；移除同步 VLM 自动命名，不保留双生产者回退。
- [ ] 4.5 将持久任务服务和 Agent worker 接入应用启动/关闭生命周期，为上传不阻塞、缺失 OpenCode、重复任务、批量部分失败、重启查询、异步自动命名和人工字段保护补充 API 测试。

## 5. 处理任务页面

- [x] 5.1 用“处理任务”替换已移除的 VLM 标注导航，接入任务列表 API，提供状态/类型筛选、稳定任务表格、关联图片、阶段、进度和最近更新时间。
- [x] 5.2 实现任务详情侧栏，展示有限结果/错误、时间、异步自动命名结果和失败语境任务重试；上传结果提供直接打开对应任务的状态入口。
- [x] 5.3 实现可见页面的活跃任务轮询、列表 skeleton、可操作空/错误状态和移动端分隔列表/drawer，不显示原始模型日志、提示词或解释性模块小字。
- [ ] 5.4 为筛选、分页、活跃刷新、终态停止轮询、上传跳转、失败重试、敏感字段隐藏和桌面/移动布局补充前端测试。

## 6. JSON-only embedding 与 v4 缓存

- [x] 6.1 调整 metadata embedding record，使其根据 sidecar 校验、语义状态和白名单文本返回显式索引资格与稳定跳过原因，移除所有文件名文本回退。
- [x] 6.2 将缓存升级到 v4；只对可索引记录调用 embedding，保存已索引/跳过总数及原因统计，并在无可索引图片时以 `no_indexable_images` 失败且不替换旧缓存。
- [x] 6.3 调整缓存加载校验，使条目集合精确匹配当前可索引图片集合，并继续校验图片、元数据和语义文本指纹；拒绝 v3 及其他旧格式缓存。
- [x] 6.4 让缓存生成任务将统计写入统一任务 `result`，并为混合资格、不可索引项不调用模型、sidecar 写回后失效、全量跳过保留旧缓存和模型切换补充测试。

## 7. 文档、迁移与验收

- [x] 7.1 更新 `.env.example`、`api.md`、`README.md` 和前端文案，移除 VLM 描述/标注说明，说明 OpenCode 预安装、固定 runtime/共享依赖、持久任务列表、异步 `auto_name`、隐私边界、批量补齐和 v4 重建顺序。
- [ ] 7.2 在测试 runtime 中完成至少一张图片从 `pending -> Agent task -> ready sidecar -> v4 embedding` 的端到端验收，并确认 Agent 无法直接修改 canonical sidecar 或应用代码。
- [x] 7.3 执行完整测试、OpenSpec 严格校验、shell 语法检查和 diff 空白检查。
