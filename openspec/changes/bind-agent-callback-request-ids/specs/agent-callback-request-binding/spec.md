## Purpose

为 Agent callback 建立由服务端控制的逻辑请求身份和权威 request ID，使同一当前 Task 执行对相同反向图片输入的网络重试只能恢复既有 provider、usage 和 operation grant 事实，而不能通过更换客户端标识产生新的外部副作用。

## ADDED Requirements

### Requirement: callback 逻辑请求身份必须由可信执行和规范化输入共同决定

反向图片 callback MUST 在当前服务认证、Task claim、scope、operation 和目标图片事实通过校验后，由服务端生成稳定的逻辑请求身份。该身份 MUST 覆盖当前 scope、Task、claim generation、attempt、允许的 operation、目标图片 SHA、实际检索图片 SHA 以及规范化后的 `search_type`、`language`、`country`、`query`、`auto_crop` 和 `refresh`；空值、大小写、外围空白和布尔值 MUST 先按既有请求规则规范化。客户端提交的 `request_id` 或 `input_digest` MUST NOT 改变该身份。

#### Scenario: 相同规范化输入得到同一逻辑身份

- **WHEN** 同一当前 Task claim 对同一目标图片提交两次只在 query 外围空白、language 大小写或等价布尔表示上不同的请求
- **THEN** 服务端将两次请求归一到同一个逻辑请求身份
- **AND** 两次请求不得产生两个 usage 或 operation grant 事实

#### Scenario: refresh 是逻辑输入而不是仅缓存提示

- **WHEN** 同一当前 claim 对相同图片和 query 分别提交 `refresh=false` 与 `refresh=true`
- **THEN** 服务端将两次请求识别为不同的逻辑输入
- **AND** 对同一个 `refresh=true` 输入的后续重试仍必须命中第一次 `refresh=true` 请求的权威事实

### Requirement: 每个逻辑请求只能绑定一个权威 request ID

系统 MUST 为每个 callback 逻辑请求持久化一个权威 `request_id`。客户端 MAY 省略 request ID；此时系统 MUST 生成不会因网络重试改变的服务端标识。客户端首次提交的合法 request ID MAY 作为兼容性提示被采纳，但同一逻辑请求随后使用另一个 request ID 时 MUST 解析为原权威事实或返回既有稳定冲突，MUST NOT 创建新的 provider、usage 或 grant。

#### Scenario: 不提交 request ID 的网络重试

- **WHEN** callback 客户端省略 request ID，第一次请求已经建立逻辑请求事实，随后以相同当前 claim 和相同输入重试
- **THEN** 系统返回第一次请求的权威 request ID 及其既有结果或进行中的既有事实
- **AND** 不按每次重试随机生成新的 request ID

#### Scenario: 相同逻辑输入更换 request ID

- **WHEN** 当前 claim 以 `request-a` 建立了某个逻辑请求，随后以 `request-b` 提交完全相同的规范化输入
- **THEN** 系统只使用 `request-a` 代表的权威 callback/usage/grant 事实，或返回既有 `unknown_execution`/稳定冲突语义
- **AND** provider 调用、usage 计量、缓存写入和 grant acquire 均不增加

### Requirement: request ID 或输入改绑必须在副作用前稳定拒绝

已存在的 request ID MUST 与其首次绑定的 Task、scope、claim generation、attempt、operation、目标和逻辑输入逐项一致；任一字段改变 MUST 返回既有 `usage_request_conflict` 或等价稳定 callback 冲突错误。已存在的逻辑请求不得因客户端提供了另一个 request ID 而被重绑定，且冲突请求 MUST 在缓存、usage、provider 和 operation policy grant 副作用前结束。

#### Scenario: 同 request ID 改 query 或目标

- **WHEN** 已完成或进行中的 `request-a` 被再次提交，但 query、实际图片 SHA、目标 SHA 或 refresh 值发生变化
- **THEN** 系统返回稳定 request conflict
- **AND** 不覆盖原结果、不创建第二条 usage 记录、不联系 provider

#### Scenario: 同 request ID 改 Task 或 claim

- **WHEN** `request-a` 被另一个 Task、scope、claim generation 或 attempt 的 callback 使用
- **THEN** 系统以不可区分的执行/请求冲突语义拒绝请求
- **AND** 不查询或泄露另一个 scope 的业务事实，不触发 grant 或 provider

### Requirement: 逻辑请求事实必须在并发和崩溃窗口中原子收束

系统 MUST 通过持久 callback 事实的原子查找/创建和数据库唯一约束，保证同一 scope、执行绑定、operation、目标和规范化输入的并发请求最终只有一个权威 request ID。callback 事实、usage 事实和 operation grant 的恢复 MUST 沿用现有事务和未知执行协议；任何已记录 provider started 但无法确认结果的请求 MUST 返回 `reverse_image_unknown_execution` 或等价既有未知状态，MUST NOT 自动重放 provider、usage 或 grant。

#### Scenario: 两个 request ID 并发首次提交

- **WHEN** 两个 Worker 同时以不同 request ID 提交相同当前 claim 和相同规范化输入
- **THEN** 只有一个 request ID 成为权威绑定，另一个请求复用该绑定或稳定失败
- **AND** 最多产生一次 provider started、一次实际 provider 调用计量和一个可执行 grant

#### Scenario: callback 事实已保存但进程随后退出

- **WHEN** 进程在保存逻辑请求事实后、返回响应前退出
- **THEN** 重试必须按持久权威 request ID 恢复 usage/grant 状态
- **AND** 不因调用方更换 request ID 而重新开始 provider

#### Scenario: provider 已开始且结果未知

- **WHEN** usage 已记录 provider started，但进程退出或网络中断导致供应商结果无法确认
- **THEN** 任意相同逻辑请求重试都返回既有 `reverse_image_unknown_execution` 和可降级信息
- **AND** 不 release grant、不重新计量、不写第二份缓存快照且不再次联系 provider

### Requirement: callback 逻辑请求必须在真实 provider、usage 和 grant 边界共用权威身份

反向图片服务 MUST 在调用缓存 miss 的 operation policy、记录 provider usage 或提交 grant idempotency key 前解析权威 request ID。缓存命中 MAY 记录独立的无 provider usage 事实，但相同逻辑请求 MUST 复用该事实；provider miss、usage event 和 `analysis.reverse_image_search` grant MUST 使用同一个权威请求关联，客户端 request ID 不得分别创建它们的幂等键。

#### Scenario: refresh miss 的重复请求

- **WHEN** `refresh=true` 的第一次请求在缓存锁内取得 grant、记录 usage 并开始 provider，随后以不同 request ID 重试
- **THEN** 重试只读取原权威 usage/grant/provider 事实
- **AND** provider 实际调用数、usage 计数和 grant acquire 数均保持一次

#### Scenario: 缓存命中的重复请求

- **WHEN** 相同当前 claim 和规范化输入已有有效缓存，客户端先后提交不同 request ID
- **THEN** 两次响应使用同一个逻辑请求权威身份并报告 provider 未调用
- **AND** 不为第二个 request ID 创建 provider grant

### Requirement: 旧执行、跨范围和新 attempt 不得互相借用 callback 事实

逻辑请求身份 MUST 绑定当前 Task 的 scope、claim generation 和 attempt；现有 callback 当前 claim/租约校验 MUST 先于逻辑请求查找和任何业务副作用。旧 claim、租约失效、错误 scope、错误 attempt 或非当前 Task 的 request ID MUST 按既有 `agent_callback_invalid_execution`/稳定冲突语义拒绝；新的有效 attempt MUST 使用新的执行绑定，不能仅凭旧 request ID 取得旧执行权。

#### Scenario: 旧 claim 更换 request ID

- **WHEN** Task 已被重新认领，旧 Worker 使用旧 claim 和一个从未出现过的新 request ID 请求相同图片
- **THEN** 系统在逻辑请求事实和 provider 路径之前拒绝 callback
- **AND** 不因新 request ID 重新建立授权、usage 或 grant

#### Scenario: 跨 scope 使用相同 request ID

- **WHEN** 另一个 scope 的 callback 使用与原请求相同的 request ID 或逻辑输入
- **THEN** 系统按当前 scope 的隔离和执行无效语义处理
- **AND** 不命中原 scope 的 callback、usage、缓存或 grant 事实

#### Scenario: 新 attempt 不能改绑旧事实

- **WHEN** 同一 Task 的新 attempt 试图使用旧 attempt 的 request ID，或旧 attempt 试图使用新 attempt 的 request ID
- **THEN** 系统拒绝改绑并保留各 attempt 的既有审计事实
- **AND** 不通过 ID 交换跨 attempt 恢复外部执行权

### Requirement: 规范化输入和目标派生必须保持现有反向图片语义

服务端 MUST 在计算逻辑请求身份前完成图片格式、图片内容、目标 SHA、受控裁剪以及 query/search 参数校验。后端确定性派生的裁剪图片 SHA、`refresh`、query 和所有影响 provider 结果的参数 MUST 纳入身份；客户端不得通过改变文件名、空值形式、未受控裁剪或非权威摘要制造第二个身份。

#### Scenario: 同一目标的受控裁剪重试

- **WHEN** 当前 claim 对同一目标整图请求相同受控裁剪参数，并以不同 request ID 重试
- **THEN** 服务端使用相同派生图片和逻辑身份恢复既有事实
- **AND** 不将同一裁剪当作第二次 provider 检索

#### Scenario: 任意图片替换或摘要伪造

- **WHEN** callback 上传的图片、source SHA、input digest 或裁剪事实与服务端 Task 目标不一致
- **THEN** 系统在逻辑请求注册前返回稳定执行无效错误
- **AND** 不读取缓存、不写 usage、不取得 grant、不调用 provider

### Requirement: local、Host、Docker、executor 和薄 CLI 必须保持协议兼容

启用 callback 的 Host、Docker 和 executor Runner MUST 继续只传递已有内部 URL、当前 Task 标识和任务级 callback 凭据；request ID MAY 省略，且响应中的权威 request ID MUST 可被薄 CLI 原样消费。无 callback 绑定的 local 直连服务 MUST 保持现有显式 request ID、缓存和 provider 兼容语义，本 change 不把 local 直连升级为公网 callback 授权路径。

#### Scenario: 薄 CLI 不提交 request ID

- **WHEN** Agent 在现有运行时环境中调用薄 CLI 且未提供 request ID
- **THEN** CLI 成功调用内部接口并输出服务端返回的权威 request ID 和既有供应商无关 JSON
- **AND** 不需要新的 secret、executor token、数据库凭据或 provider 凭据

#### Scenario: local 直连回归

- **WHEN** local 测试直接构造无 callback binding 的反向图片请求并使用已有 request ID 或省略该字段
- **THEN** 请求继续遵循现有缓存命中、refresh 和 provider 语义
- **AND** callback 逻辑绑定表不会把 local 直连请求误判为 Agent 执行
