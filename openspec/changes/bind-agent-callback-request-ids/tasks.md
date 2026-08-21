## 1. 逻辑请求身份与 callback 契约

- [x] 1.1 在公共 callback 协议中定义反向图片逻辑请求的规范化输入结构和稳定 64 位摘要，覆盖当前 scope、Task、claim generation、attempt、operation、目标/实际图片 SHA、search type、language、country、query、auto_crop 和 refresh；保持摘要字段边界和中文 docstring 与实现一致。
- [x] 1.2 修改 callback request 校验流程，使服务端在可信 Task/目标校验后重新计算摘要；客户端 `input_digest` 只能作为一致性声明，request ID 只能作为兼容性提示，不能覆盖服务端事实。
- [x] 1.3 定义 request ID 解析结果和稳定错误映射：省略 ID 时生成确定性权威 ID，同逻辑输入更换 ID 复用权威行或已有 unknown/conflict，已存在 ID 改输入返回 `usage_request_conflict`；保持 `agent_callback_invalid_execution` 不泄露 Task、scope 或 claim 原因。

## 2. 持久事实与并发约束

- [x] 2.1 在内存和 PostgreSQL callback repository 中实现“按 request ID 查改绑、按完整逻辑事实查权威、未命中才创建”的双索引语义，并让并发唯一冲突重新读取竞争者事实，不生成第三个 request ID。
- [x] 2.2 为现有 `agent_callback_requests` 的 scope、Task、claim、attempt、operation、target SHA 和 input digest 增加前向复合唯一索引/约束；在迁移中先检测历史重复逻辑键，发现重复时停止并报告，不删除、合并或猜测覆盖既有事实。
- [x] 2.3 增加必需 callback schema/唯一索引的启动或请求前检查；约束未就绪、事实字段缺失或历史绑定无法证明一致时 callback fail-closed，local 无 callback 直连路径不被错误阻断。
- [x] 2.4 保持 `ReverseImageUsageEvent` 的完整绑定比较和既有唯一 request ID 约束，验证 callback resolver 返回的权威 ID 才能进入 usage 与 operation grant；禁止通过新 ID 绕过 terminal/unknown association。

## 3. 反向图片服务接入权威 ID

- [x] 3.1 调整反向图片 API/service 的顺序：先规范化请求、验证当前 claim/Task/目标和受控裁剪，再解析 callback 逻辑身份；不得在缓存、usage、provider 或 policy acquire 前接受客户端自报摘要。
- [x] 3.2 将 canonical request ID 贯穿缓存锁内的 usage create/finish、provider-started 记录和 `analysis.reverse_image_search` grant idempotency key；缓存命中不 acquire provider grant，refresh miss 的重复提交不重复 provider、usage 或 grant。
- [x] 3.3 处理 callback row 已保存、usage 尚未保存、usage 已完成、grant 为 terminal/unknown、provider started 未知和 provider 明确失败等崩溃/恢复分支；结果未知时返回既有 `reverse_image_unknown_execution`，不 release 或自动重放。
- [x] 3.4 保持 query、language、country、search_type、auto_crop、refresh 的既有外部语义和稳定错误；同 ID 改任一影响结果的参数冲突，不同 ID 的同规范化输入只恢复同一权威事实。
- [x] 3.5 更新 API 错误映射和响应，返回服务端权威 request ID 及既有供应商无关结果；不新增 request-ID 专用错误码，不泄露内部 callback 行、grant、scope 或 provider 细节。

## 4. 运行时与恢复兼容

- [x] 4.1 保持 Host、Docker、executor 的 callback URL、Task ID、任务级凭据和 secret 隔离不变；让薄 CLI 默认省略 request ID、继续支持可选旧参数，并原样输出服务端权威 ID。
- [x] 4.2 验证旧 claim、租约失效、错误 scope、跨 Task、跨 attempt 和重复 ID 的校验顺序，确保逻辑事实查找前返回稳定执行无效/冲突且零 provider、usage、grant 副作用。
- [x] 4.3 保持无 callback binding 的 local 直连模式现有显式 request ID、缓存、refresh 和 provider 兼容行为；不把 local 直连纳入 Agent callback 事实表或新增限流/quota 语义。
- [x] 4.4 更新反向图片内部接口参考文档和必要的 Runner/CLI 说明，记录 request ID 可省略、服务端权威返回、重试/unknown 语义和禁止 Agent 直连 provider 的边界；新增注释和 docstring 使用中文。

## 5. 测试与安全验收

- [x] 5.1 增加 callback 单元测试，覆盖规范化 query/language/country/布尔值、图片/裁剪 SHA、refresh 纳入逻辑键、客户端摘要伪造、同 ID 改输入和同逻辑输入换 ID。
- [x] 5.2 增加内存事实层测试，覆盖省略 ID、权威 ID 复用、不同 ID 并发首次提交、跨 Task/scope/attempt、terminal/unknown 状态以及唯一冲突后的竞争者恢复。
- [x] 5.3 增加 PostgreSQL migration/集成测试，覆盖复合唯一索引、历史重复逻辑键阻断、两个独立 session 的并发竞态、scope 隔离和字段缺失 fail-closed。
- [x] 5.4 增加反向图片服务测试，使用 fake provider 和 fake policy 断言相同 `refresh=true`/query 输入更换 ID 只产生一次 provider/usage/grant，缓存命中零 provider grant，改输入稳定冲突，provider started 未知不重放。
- [x] 5.5 增加 API/ASGI/CLI/Runner 兼容测试，覆盖认证前 body 边界、当前 claim、request ID 可选、权威响应、Host/Docker/executor 环境不新增 secret，以及 local 模式回归。
- [x] 5.6 运行与改动风险相称的 Python、PostgreSQL（可用时）、反向图片、任务恢复和 OpenSpec 回归；执行 `openspec validate --strict`、compileall、格式检查、`git diff --check` 和敏感字段/secret 扫描，确认不引入公网限流、quota owner 或服务端专属商业术语。
