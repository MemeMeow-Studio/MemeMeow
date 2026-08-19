## MODIFIED Requirements

### Requirement: OpenCode 运行时必须复用且隔离图片上下文
系统 MUST 在固定 runtime 中执行已安装的 OpenCode，所有图片 job MUST 复用预安装的 Node.js 依赖，且任务执行期间 MUST NOT 调用包管理器或为每个 job 下载依赖。系统 MUST 通过 `opencode-workspace` capability 从可信任务上下文选择受控 workspace；所有 workspace MUST 复用同一 OpenCode DB，但分别使用自己的 `--dir`、工作目录和不含密钥的 `opencode.json`。配置 MUST 使用 `@ai-sdk/openai` Responses provider 且只注册 `gpt-5.6-luna`；服务地址 MUST 从 `MEMEMEOW_OPENCODE_BASE_URL` 的受控运行环境引用，认证值 MAY 通过 `MEMEMEOW_OPENCODE_API_KEY` 注入，但在 executor 或非 local provider 模式下该值 MUST 是绑定当前 Task/claim 的短期 capability，MUST NOT 是长期 provider key；长期 key 只能由独立的后端 broker 持有。模型 MUST 由 `MEMEMEOW_OPENCODE_MODEL` 经命令行传递，并固定传入 `--variant max` 以使用 `max` 推理强度。每张图片 MUST 使用独立的 OpenCode session，后一张图片不得继承前一张图片的会话内容；重试或 resume MUST 保持原 session 与可信 workspace 的绑定。系统 MUST 通过配置的并发上限和跨进程 slot 互斥限制同时运行的语境生成子进程；默认上限 MUST 为 `1`，未验证共享 runtime/DB 并发安全时不得超过已验证上限。

#### Scenario: 默认配置保持单并发
- **WHEN** 未配置 OpenCode 并发上限或显式设置为 `1`
- **THEN** 系统一次只运行一个语境生成子进程，并保持现有稳定排队顺序

#### Scenario: 不同图片在安全上限内并行
- **WHEN** 多个不同图片的语境生成 job 同时处于 `queued`，且运行时已验证支持配置的并发上限
- **THEN** 系统最多同时运行该上限数量的 OpenCode 子进程，每个 job 使用独立 session，任一 job 的模型上下文不得进入另一 job

#### Scenario: 不同 workspace 复用同一运行时
- **WHEN** 多个 job 的可信上下文选择不同 workspace
- **THEN** 系统使用相同 OpenCode DB 和预安装依赖、不同工作目录与配置执行任务，session 和图片上下文不得跨 workspace 恢复

#### Scenario: 并发资源达到上限
- **WHEN** 活跃语境生成子进程数量已达到配置上限
- **THEN** 后续 job 保持 `queued`，不启动额外子进程、不重复调用外部检索服务，并在资源释放后按稳定顺序继续调度

#### Scenario: 同一图片重复提交
- **WHEN** 同一相对路径和图片 SHA-256 已有 `queued` 或 `running` 的语境生成 job
- **THEN** 系统返回现有 job 标识，不创建第二个 job，也不启动第二次 OpenCode 或同键反向图片检索调用

#### Scenario: 并行任务目标发生变化
- **WHEN** 任一并行 job 完成前图片被删除、重命名或内容变化，导致路径或 SHA-256 与提交记录不一致
- **THEN** 该 job 进入 `failed` 并返回 `target_changed`，不得把结果写入其他图片或新内容

#### Scenario: 语境写回后合并索引失效
- **WHEN** 一批并行语境 job 分别成功提交数据库语境
- **THEN** 系统可记录每张 Meme 的索引失效，但不得为每张 Meme 立即重建当前 scope 的 embedding；上层显式触发的索引任务必须基于一致的已提交数据库快照生成结果
