## Context

当前 `/internal/reverse-image/search` 与 `/internal/visual-search/match` 绕过请求 scope resolver，并由 `task_id` 经全局任务控制面恢复 `Task.scope_id`。这条方向是正确的，但路由没有服务间认证：反向图片路由还会在 Task 校验前读取 multipart 图片；两个服务只检查 `running` 和部分租约字段，调用方不能证明自己属于当前 claim，`claim_generation == 0` 也可能绕过租约检查。Compose 内网和未公开端口只能减少暴露面，不能在 Agent 拥有开放网络与命令执行能力时充当授权。

`Task.scope_id`、递增 claim generation、lease owner/expiry、目标 Meme/SHA 和图片处理 attempt 已经是持久化事实来源。`make-application-scope-aware` 另行要求 factory 返回的整组 scope 服务与 resolver/Task scope 一致；本设计复用这些边界，不引入用户、账户或商业授权实体。详见 [proposal.md](proposal.md) 的 Why。

## Goals / Non-Goals

**Goals:**

- 在任何业务请求体解析和 Task 查询前验证 Agent 服务身份，并在业务副作用前验证当前 Task 执行权。
- 让 callback 权限绑定单个 Task、claim、attempt、允许操作和目标图片版本，使旧 Worker、泄露的 task id 或跨任务重放失效。
- 为反向图片、视觉匹配和后续 Agent callback 提供一个默认 fail-closed 的注册与装配模式。
- 保持用户鉴权、服务间认证、operation policy 和 provider 凭据为相互独立的信任域。
- 允许开源入口使用显式安全默认实现，适配宿主替换底层 verifier 而不改变核心 Task/scope/目标校验。

**Non-Goals:**

- 不在开源核心实现 User、Session、Subscription、Plan、Payment 或额度周期。
- 不用 callback 凭据替代普通公网 API 的用户认证，也不用用户 session 认证内部 Agent。
- 不把 Agent 容器视为完全可信，不承诺仅凭 Compose 网络阻止其它出网行为。
- 不重新设计 Agent 结果 artifact schema、operation grant 生命周期、反向图片 provider 或视觉相似度算法。

## Decisions

### 1. 服务认证与 Task 执行授权是两个必须同时成立的条件

callback 验证分成两个逻辑层次：服务认证证明请求来自允许的 Agent 执行环境；执行绑定证明这次调用属于一个具体 `task_id` 的当前 claim、attempt、目标和允许操作。线上可以把两层编码成一个任务级不透明凭据，也可以使用 mTLS/服务 token 加单独的任务执行凭据，但 verifier 输出的可信上下文必须同时包含两层结论。

执行上下文至少绑定协议版本、issuer/audience、服务身份、`task_id`、`scope_id`、claim generation、lease owner 的不可伪造引用、attempt id、允许的 callback operation、目标 SHA、签发/过期时间及防重放标识。静态服务 token 只能证明服务身份，不能单独授权任意 Task；客户端提交的普通 Header 或 body claim 字段也不能补足执行授权。

开源提供显式配置的默认发行与验证实现，并允许宿主注入等价 verifier。默认实现的根 secret 不进入 OpenCode 子进程；Runner 为已认领的 Task 签发或取得最小任务级凭据，只把该凭据交给对应执行。API 到 executor 的 Bearer token 不复用为 callback token，因为两条链的调用方向、受众和泄露影响不同。

只验证服务 token 的替代方案仍允许同一 Agent 借用其它活动 task id。只验证任务字段而没有服务认证则允许公网探测并放大请求，因此两者都不采用。

### 2. 在 ASGI 请求体解析前完成第一层认证

所有 Agent callback 路由必须进入统一的 fail-closed 前置层。该层先验证凭据格式、签名/通道身份、audience、有效期和允许的 callback 类别，并应用 Header 与声明大小限制；失败时在读取 multipart/JSON body 和查询 Task 前返回统一 `agent_callback_unauthorized`。路由启用但 verifier 缺失、secret 为空、验证异常或 callback 未注册时一律拒绝，不能回退到内网信任、local scope 或用户认证。

第一层通过后才按路由上限读取请求体，并要求请求中的 `task_id`、operation 和 request id 与认证上下文一致。随后通过全局任务控制面查询 Task。这样既避免未认证调用者利用不同错误探测 Task，又避免大 multipart 在认证前占用内存、临时文件或图片解析资源。

仅在 handler 内加 token dependency 的替代方案会让框架先解析 multipart；为每个路由手写验证又容易漏掉新入口，因此采用统一前置层加显式 callback 注册表。

### 3. Task 当前 claim 是 scope 恢复和业务授权的权威事实

服务认证通过后，验证顺序固定为：

1. 认证上下文与请求的 task/operation/request 绑定一致；
2. Task 存在且类型允许该 callback；
3. Task 为 `running`，claim generation 大于零，generation 与 owner 匹配且租约未过期；
4. Task 的 `scope_id`、attempt、目标 Meme/SHA 与执行绑定一致；
5. 从持久 `Task.scope_id` 构造 `ScopeServices`，并对外层对象及 callback 将使用的子服务执行 scope 一致性校验；
6. 重新读取当前 Meme/产物并验证目标 SHA，再进入具体业务和 operation policy。

任何失败都返回对 Agent 不可区分的 `agent_callback_invalid_execution`，详细原因只进入脱敏服务端诊断。scope 装配失败不得调用只按 task id 的无 fencing 失败写回，也不得终止另一 Worker 的当前 claim；仅持有完整当前 claim 的路径才能收束自己的 Task。

任务重新认领会递增 generation，因此旧执行凭据即使尚未到期也会在数据库比较时失效。凭据过期时间不得晚于其租约边界；heartbeat 延长租约时是否续签凭据由发行器决定，但每次 callback 仍以数据库当前事实为准。

### 4. callback operation 必须绑定明确目标和最小输入

视觉匹配 callback 从 Agent Task 恢复目标 Meme 与 SHA，并同时验证当前 Meme SHA、查询视觉 embedding SHA 和任务目标 SHA；三者任一不一致都拒绝，不能只证明 embedding 与当前 Meme 一致。

反向图片 callback 不再允许 Agent 借一个 `auto` Task 上传任意图片。整图请求必须与 Task 目标 SHA 一致；裁剪需求由后端从受控源图按受限区域生成，或使用后端已签发且绑定该 Task/claim/源 SHA 的派生图引用。第一版优先采用服务端裁剪参数，避免新增派生图授权实体。缓存键和 usage input digest 使用经过绑定验证后的实际检索内容，但授权归属始终是源 Task、scope 和目标 SHA。

后续 callback 必须在注册时声明允许的 Task 类型、operation、请求体上限、是否产生外部副作用和目标验证器。没有完整声明的路由不可启用。

### 5. request id 与当前执行共同提供重放和幂等边界

每次 callback 使用服务端格式校验的 request id，并将其与 `task_id`、scope、claim generation、attempt、operation、目标 SHA 和规范化输入摘要关联。相同绑定的重复请求复用同一结果或持久事实；任一字段冲突返回 `agent_callback_invalid_execution`。provider 调用开始、usage、缓存写入和状态推进继续遵守各自现有事务/未知执行协议，callback 层不通过生成新 request id 掩盖不确定结果。

纯只读视觉匹配也受当前 claim 和请求大小/频率上限保护；是否持久化其完整响应可以按实现成本决定，但同一 request id 不得被改绑。产生计量或写入的 callback 必须持久化幂等事实后才能执行外部副作用。

仅依赖 token nonce 拒绝所有重复请求会破坏网络重试；只依赖业务 `request_id` 又会让旧 claim 重放，因此使用当前执行绑定加业务幂等事实。

### 6. 用户鉴权、operation policy 和 callback verifier 独立装配

公网请求先由宿主用户认证解析用户所属 scope；Agent callback 不携带用户 session，而是从受验证 Task 恢复 scope。callback 通过认证和 Task 授权后，仍需在真实 provider 边界调用 `analysis.reverse_image_search` 等 operation policy；callback 凭据不能充当 grant，grant 也不能充当 callback 凭据。

适配宿主可以把服务认证替换为轮换 token、HMAC 或 mTLS，但必须保留任务执行绑定和核心数据库复核。开源 `AllowAllOperationPolicy` 只影响 operation 可用性，不放宽 callback 认证。供应商密钥和数据库凭据始终停留在后端，不能放入 callback token 或 Agent 环境。

所有依赖继续通过 `make-application-scope-aware` 建立的同一个 `create_app` keyword-only 边界装配：先验证 scope resolver/factory 及 scope 一致性，再装配 operation policy 与 callback verifier/issuer，最后注册路由并启动 Worker。callback change 不创建第二套应用工厂、模块全局 verifier 或隐式 local/allow-all 回退。

### 7. 错误与可观测性避免存在性和凭据泄漏

外部只暴露 `agent_callback_unauthorized` 与 `agent_callback_invalid_execution` 两类认证/执行错误，不区分 task missing、跨 scope、类型不符、旧 generation、租约过期或目标变化。响应不包含 scope、claim、Task 状态、verifier 原因或凭据片段。

服务端安全日志记录 request correlation、路由 operation、脱敏服务身份、服务端 Task 摘要和稳定内部原因；原始 token、mTLS 证书正文、用户信息、图片正文、operation grant 和 provider secret 一律不记录。认证失败的高基数 task id 不作为无限标签进入指标。

## Risks / Trade-offs

- [Risk] callback secret 进入 Agent 后可能被该次任务读取。→ 只签发绑定单 Task/claim/operation/目标且短期有效的最小凭据；根 secret、executor token 和其它任务凭据不进入子进程。
- [Risk] callback 第一层认证与 FastAPI body 解析顺序实现错误，仍可能在认证前读取大文件。→ 使用 ASGI 级前置测试，以计数 receive/临时文件/Task 查询断言未认证请求零读取、零业务访问。
- [Risk] Task lease 在长调用中到期会拒绝合法 callback。→ Runner 保持 heartbeat，凭据有效期不超过租约；拒绝后由持久 Task 恢复协议重新认领，不延长旧 claim。
- [Risk] 凭据轮换使在途 Agent 失败。→ verifier 支持受限双 key 验证窗口，但数据库当前 claim 仍是最终授权；轮换窗口不允许扩大 task/operation 范围。
- [Risk] 服务端裁剪与现有 Agent 上传裁剪图不兼容。→ 先迁移 CLI 为传递受限裁剪区域或后端派生引用，再强制认证；不得保留任意图片作为兼容旁路。
- [Risk] 通用 callback 安全能力与反向图片基础 capability 实施顺序错误。→ 已直接修订尚未归档的 `add-task-scoped-reverse-image-search`，使其依赖本安全边界；先完成 `make-application-scope-aware` 的 claim/factory 加固，再联合实施两项 change，先归档反向图片基础 capability，随后归档本通用安全 capability。

## Migration Plan

1. 先完成 `make-application-scope-aware` 的完整 claim fencing 与 factory scope 一致性任务；在此基础上增加 callback 凭据模型、显式 verifier 装配和路由注册表，用拒绝测试确认缺失/错误配置 fail-closed，但暂不对生产 Agent 开放新入口。
2. 让 Task claim/attempt 创建路径签发最小执行绑定，并更新 Runner、executor 与两个 Skill CLI 安全传递凭据；确认日志、任务 payload 和结果 artifact 不含 secret。
3. 在视觉匹配和反向图片业务调用前接入统一验证顺序、scope 服务一致性、目标 SHA 和 request id 绑定；将反向图片裁剪迁移为服务端受控派生。
4. 在同一发布中启用强制 callback 认证并删除无认证旁路。发布前已经运行但没有执行凭据的 Agent Task 以可诊断失败收束或由新 claim 重新执行，不为旧 claim 补发宽松凭据。
5. 增加密钥轮换、旧 claim、跨 scope、目标替换、重放和认证前大 body 拒绝观测，再在适配宿主注入生产 verifier；归档时先落地反向图片基础 capability，再归档本通用安全 capability。

回滚时关闭 Agent callback 和新的 Agent 任务调度，保留 Task、usage 和执行审计；不得通过恢复无认证路由维持可用性。只有恢复到同时携带有效服务认证和当前执行绑定的版本后才能重新开放 callback。
