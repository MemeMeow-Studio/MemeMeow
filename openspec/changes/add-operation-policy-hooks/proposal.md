## Why

宿主部署需要按 scope 判断图片上传、Agent 分析和联网反向图片检索是否可用，并在真实副作用发生时原子计量；这些判断不能只包在宿主 HTTP 路由外层，否则会漏掉合集导入、图片处理 Worker 恢复和缓存未命中的 provider 调用。开源核心需要提供不理解用户、订阅或支付的通用 policy 边界，并由显式的 allow-all 实现保持现有单用户行为。

## What Changes

- 新增可注入的 operation policy 协议，支持非权威查询、原子取得 grant、提交计量和释放 reservation。
- 固化首期 operation 标识：`image.upload`、`analysis.agent`、`analysis.reverse_image_search` 和 `image.delete`。
- 在普通上传、合集导入的新图片、图片处理 Worker 创建和执行 Agent Task、反向图片缓存未命中后的真实 provider 调用处接入统一生命周期。
- 将有效产物检查和活动 Agent Task dedupe 放在 acquire 之前；Worker 自动重试、租约恢复和同一逻辑 Agent Task 的重复处理不重复扣费，用户主动重试创建新 job revision 和新 Agent Task 并重新取得 grant。
- 将 Agent grant 绑定到服务端持久化的 `meme_context_generation` Task；execution attempt 只引用该 Task/grant 并记录自己的 request/session 与输入摘要。外部结果未知时不自动重放，也不释放已经 commit 的 grant。
- policy 拒绝 Agent 分析时，以通用 `blocked` 状态和稳定原因保留图片处理诊断；`analysis.reverse_image_search` 被 operation policy 明确拒绝时，`auto` 策略降级为离线分析，不静默改为普通网页搜索。provider 未配置、服务故障或协议错误仍遵守反向图片 capability 的既有失败语义。
- 固定核心稳定原因 `operation_forbidden`、`operation_limit_exceeded` 和 `operation_policy_unavailable`；policy 可以提供可选 `retry_at`，但核心不理解额度周期，也不据此自动重试。
- 开源入口显式安装 `AllowAllOperationPolicy`；不新增账户、认证、订阅、套餐、额度数值、支付或计费数据库。
- 增加拒绝与 `blocked`、并发 reservation、缓存命中、批量部分成功、任务恢复、`retry_at` 仅提示和未知外部结果不重放的测试。

## Capabilities

### New Capabilities

- `operation-policy`: 为 scope-bound 公共核心定义 operation vocabulary、policy/grant 生命周期、接入边界和拒绝后的稳定行为。

### Modified Capabilities

无。现有图片、任务和反向图片规范在默认 allow-all policy 下保持原有行为；本 change 的跨流程拒绝和计量契约集中在新的 capability 中。

## Impact

- 受影响的公共代码包括上传与合集导入编排、图片处理 job/Worker、持久 Agent Task 与 OpenCode 执行边界、`ReverseImageService` 及应用级服务装配。
- 需要在 Agent Task 的可信元数据或关联记录中持久化不透明 grant 引用，但不定义宿主的配额存储格式。
- 宿主仓库只需注入认证后的 scope 和 entitlement/quota policy；公共核心不读取客户端的 scope、用户、套餐或余额字段。
- 外部 API 可增加 capability availability 查询和稳定拒绝错误，但不得暴露其他 scope、grant、供应商密钥或商业内部字段。
