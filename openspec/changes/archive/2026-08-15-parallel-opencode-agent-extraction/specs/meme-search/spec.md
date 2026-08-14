## ADDED Requirements

### Requirement: OpenCode 运行时必须复用且隔离图片上下文
系统 MUST 在固定 runtime 中执行已安装的 OpenCode，所有图片 job MUST 复用同一套受控配置和预安装的 Node.js 依赖，且任务执行期间 MUST NOT 调用包管理器或为每个 job 下载依赖。系统 MUST 在 `<runtime>/workspace/opencode.json` 维护不含密钥的 `@ai-sdk/openai` Responses provider 配置，其中只注册 `gpt-5.6-luna`；服务地址和密钥 MUST 分别从 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 的环境变量引用，模型 MUST 由 `MEMEMEOW_OPENCODE_MODEL` 经命令行传递，并固定传入 `--variant max` 以使用 `max` 推理强度。每张图片 MUST 使用独立的 OpenCode session，后一张图片不得继承前一张图片的会话内容。系统 MUST 通过配置的并发上限和跨进程 slot 互斥限制同时运行的语境生成子进程；默认上限 MUST 为 `1`，未验证共享 runtime/DB 并发安全时不得超过已验证上限。

#### Scenario: 默认配置保持单并发
- **WHEN** 未配置 OpenCode 并发上限或显式设置为 `1`
- **THEN** 系统一次只运行一个语境生成子进程，并保持现有稳定排队顺序

#### Scenario: 不同图片在安全上限内并行
- **WHEN** 多个不同图片的语境生成 job 同时处于 `queued`，且运行时已验证支持配置的并发上限
- **THEN** 系统最多同时运行该上限数量的 OpenCode 子进程，每个 job 使用独立 session，任一 job 的模型上下文不得进入另一 job

#### Scenario: 并发资源达到上限
- **WHEN** 活跃语境生成子进程数量已达到配置上限
- **THEN** 后续 job 保持 `queued`，不启动额外子进程、不重复调用外部检索服务，并在资源释放后按稳定顺序继续调度

#### Scenario: 同一图片重复提交
- **WHEN** 同一相对路径和图片 SHA-256 已有 `queued` 或 `running` 的语境生成 job
- **THEN** 系统返回现有 job 标识，不创建第二个 job，也不启动第二次 OpenCode 或同键反向图片检索调用

#### Scenario: 并行任务目标发生变化
- **WHEN** 任一并行 job 完成前图片被删除、重命名或内容变化，导致路径或 SHA-256 与提交记录不一致
- **THEN** 该 job 进入 `failed` 并返回 `target_changed`，不得把结果写入其他图片或新内容

#### Scenario: 语境写回后合并缓存失效
- **WHEN** 一批并行语境 job 分别成功写回 sidecar
- **THEN** 系统可记录每张图片的缓存失效，但不得为每张图片立即重建全库 embedding；上层显式触发的缓存任务必须基于一致的已提交 sidecar 快照生成结果

### Requirement: Agent 输出必须经过后端解析与校验后写回
系统 MUST 把 OpenCode 事件流、工具输出、搜索结果和最终 assistant 文本视为不可信数据。系统只可通过临时 loopback headless server 的公开 session messages API 从成功完成的 session 取得最后一条完整 assistant 文本，不得使用会内联附件并截断 stdout 的 CLI export；只接受一个原始 JSON 对象或唯一的 JSON fenced block，并在输出 schema、sidecar 字段模型、目标相对路径和图片 SHA-256 全部校验通过后原子写回。Agent MUST NOT 直接写入 canonical sidecar。并行执行不得改变这些校验和每张图片的提交顺序。

#### Scenario: 并行任务分别安全写回
- **WHEN** 两个不同图片的 Agent 输出均通过 schema、sidecar 字段和目标 SHA-256 校验
- **THEN** 系统分别原子写回对应 sidecar，保存各自 session ID 和结果哈希，且任一写回不会覆盖另一图片的结果

#### Scenario: 并行任务中一个输出失败
- **WHEN** 一个 job 超时、输出无效或 schema 校验失败，而其他 job 仍在运行
- **THEN** 失败 job 进入稳定的 `failed` 状态且不写入候选字段，其他 job 可以独立继续并完成
