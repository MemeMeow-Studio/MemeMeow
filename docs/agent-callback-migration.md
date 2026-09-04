# Agent callback 迁移与回滚

本文说明内部 Agent callback 从旧的“只凭 task id 和内网地址”迁移到服务身份、当前
Task claim 和目标图片三者共同授权后的部署顺序。callback 不是用户登录接口，也不是
operation grant；它只证明一次受控 Agent 执行可以调用指定的内部能力。

## 发布前检查

1. 为 API 配置 `MEMEMEOW_AGENT_CALLBACK_SECRET`。密钥必须是随机、非空且至少 16 字节；
   空值、控制字符、过短值或 verifier 初始化失败都会让 callback fail-closed。
2. 确认 Agent executor 使用独立的 `MEMEMEOW_AGENT_EXECUTOR_TOKEN` 或 named volume
   token。executor Bearer token、callback 根 secret、`SERPAPI_API_KEY`、数据库凭据和
   operation grant 不得相互复用。
3. 先运行数据库迁移和 callback 拒绝回归，再启动 Agent 调度。`0015_bind_agent_callback_request_ids`
   会先检测 `agent_callback_requests` 的历史重复逻辑键和不完整绑定；发现异常时停止迁移，
   不删除、合并或猜测覆盖既有事实。Runner 只能拿到当前
   `task_id`、claim generation/attempt、目标 SHA、允许 operation 和最长两小时有效的
   callback token；每次调用仍以数据库中的当前 claim 和未过期 lease 为准。
4. 反向图片 Agent 只通过 `/internal/reverse-image/search` 薄客户端调用；`request_id` 可以
   省略，服务端按当前 scope、Task claim、attempt、目标/实际图片 SHA 和规范化检索输入
   返回唯一权威 ID。`input_digest` 如果由旧客户端提交只作为一致性声明，不能覆盖服务端重算值。
   `forbid` 不
   读取缓存、不 acquire、不联系 provider；`auto` 仍须经过当前 callback 校验和
   `analysis.reverse_image_search` acquire。SerpApi 密钥只留在 API。

旧的在途任务不补发宽权限 token。没有当前完整 claim 的任务应以
`agent_callback_invalid_execution` 或稳定任务失败收束，随后由显式重试创建新的 claim；
不能使用旧 `task_id`、旧 token 或新 request ID 继续执行；不同 ID 不能把同一逻辑输入
改绑成第二个 callback、usage、provider 或 grant。

## 密钥轮换

轮换使用短暂双 key 验证窗口：

```text
MEMEMEOW_AGENT_CALLBACK_SECRET=<new-secret>
MEMEMEOW_AGENT_CALLBACK_VERIFICATION_KEYS=old-kid=<old-secret>
```

新 token 只用当前根 secret 签发，旧 key 只用于验证已经发出的短期 token。先部署能够
验证新旧 key 的 API，再滚动重启 Runner/executor 使其取得新 token；确认旧 token 的两小时
最大验证窗口已结束后，删除 `MEMEMEOW_AGENT_CALLBACK_VERIFICATION_KEYS` 并再次重启 API。
不要把 secret 写入 `.env` 提交、任务 payload、结果 artifact、缓存、usage、日志或
OpenCode prompt。轮换期间仍以 PostgreSQL 当前 claim、owner、lease、attempt 和目标
SHA 为最终授权，验证窗口不会扩大 Task 或 operation 范围。

executor token 单独轮换：停止 API 和 executor，备份并保留任务/数据库，替换 named
volume 中的 0600 token，再先启动 executor 健康检查、后启动 API。不要把 token 复制到
checkout，也不要用 callback secret 替代 executor token。

## 旧任务收束

发布前停止新 Agent claim 后，按以下顺序处理旧执行：

- 让已持有有效 claim 的任务完成或在租约结束前取消；callback 认证失败不得触发 provider、
  usage、缓存、Meme、向量或阶段写回。
- 对 `prepared` 且未发生副作用的任务可由恢复器 release；已经 commit、`external_started`
  或结果未知的 attempt 保留计量事实，标记 `unknown_execution`，禁止自动重放。
- 旧 claim 重新认领时 generation 必须递增。旧 Runner 即使 token 尚未过期，也会因
  generation/owner/lease 校验失败；用户主动重试才创建新的 job revision、Task、attempt
  和 grant。
- 迁移完成后扫描日志、任务结果和 Agent 环境，确认没有根 secret、executor token、
  provider secret、绝对宿主路径或其它 scope 字段。

## 禁用式回滚

callback 版本出现故障时，回滚步骤是“禁用 callback 和新 Agent 调度”，不是恢复无认证
路由：

1. 停止新的图片处理/Agent claim，保留 PostgreSQL Task、attempt、usage、grant 和结果
   artifact；不要清空表或把未知执行改成可重试成功。
2. 让 `/internal/reverse-image/search` 在没有完整服务凭据和当前 claim 时继续返回稳定
   拒绝；网络隔离和端口隐藏不能替代认证。视觉候选没有 callback 回滚路径。
3. 只在恢复到同时支持服务认证、当前 claim fencing、scope/目标 SHA 校验的版本后重新
   开放 callback。旧任务由新 claim 或显式重试收束，不为旧执行恢复兼容旁路。

普通上传、已有缓存命中、本地视觉搜索和单图文本 embedding 不应因 callback 回滚而被
   伪装为 Agent 成功。联网反向图片调用在 provider 已开始但结果未知时保持
`reverse_image_unknown_execution`；`auto` 任务可以继续离线分析，但不得把未知子调用
冒充整个 Agent Task 成功。

## 验收边界

本地可运行的验收应至少覆盖 callback 认证前拒绝大 body、旧 claim/跨 scope/目标替换、
省略 ID、同逻辑输入换 ID、同 ID 改输入、Agent 环境脱敏、反向图片 `forbid/auto` 和 provider unknown。真实
Compose、外部模型、视觉权重及供应商调用需要在具备 Docker、凭据和模型资源的 staging/
发布环境执行；缺少这些条件时保留对应 OpenSpec 任务未勾选。
