## 1. Callback 认证契约与应用装配

- [x] 1.1 定义 Agent callback 服务认证、任务执行绑定、可信验证上下文和 `agent_callback_unauthorized`/`agent_callback_invalid_execution` 稳定错误；普通请求字段不得构造或覆盖可信上下文。
- [x] 1.2 实现显式配置的开源凭据发行与验证能力，支持协议版本、issuer/audience、服务身份、过期和 key id；根 secret 为空、格式错误或 verifier 异常时必须 fail-closed。
- [x] 1.3 在应用工厂增加独立于 request scope resolver、用户认证和 operation policy 的 callback verifier/issuer 注入点，并建立声明 Task 类型、operation、body 上限、副作用类型和目标验证器的 callback 注册表。
- [x] 1.4 增加脱敏安全日志与受限指标，禁止记录原始 callback 凭据、executor token、operation grant、用户信息、其它 scope、图片正文和 provider secret。

## 2. Task Claim 执行绑定

- [x] 2.1 仅在 Task 已取得完整非零 claim 后，为该次执行签发或取得绑定 `task_id`、`scope_id`、claim generation/owner、attempt、允许 operation、目标 SHA 和有效期的最小 callback 凭据；凭据有效期不得越过租约授权边界。
- [x] 2.2 更新 host、Docker 和 executor Agent Runner，只向对应 OpenCode 执行传递 callback 地址与任务级凭据；API 到 executor 的 Bearer token、根 secret、用户 token、grant、数据库和 provider 凭据不得进入子进程。
- [x] 2.3 实现 callback 的 Task 权威复核：校验允许类型、`running`、非零当前 generation、owner、未过期 lease、attempt、scope 和目标，并在重新认领、取消或终态后拒绝旧执行绑定。
- [x] 2.4 从持久 `Task.scope_id` 构造 `ScopeServices`，复用 factory scope 一致性校验覆盖外层及 callback 使用的子服务；装配失败不得回退 local，也不得以不完整 claim 终止其它 Worker 的任务。

## 3. 路由前置与目标约束

- [x] 3.1 在 ASGI 请求体解析前实现统一 callback 前置认证；未认证、未注册或 verifier 缺失时，在读取 multipart/JSON、查询 Task、创建临时文件和业务访问前拒绝。
- [x] 3.2 为 callback 请求增加 Header/声明/body 大小限制和受验证的 task/operation/request id 一致性检查；认证与执行错误响应不得泄露 Task 存在性、scope、类型、状态或 claim 原因。
- [x] 3.3 将 `/internal/visual-search/match` 接入统一边界，并同时校验 Agent Task 目标 SHA、当前 Meme SHA 和视觉 embedding SHA 后才返回当前 scope 候选。
- [x] 3.4 将 `/internal/reverse-image/search` 与薄 CLI 接入统一边界，整图必须匹配 Task 目标 SHA；将裁剪迁移为后端基于受控源图和受限参数生成或等价的任务绑定派生图，删除任意图片兼容旁路。
- [x] 3.5 检查所有现有及后续 Agent callback 入口，确保未在注册表声明完整安全约束的路由不可启用，普通公网 scope middleware 不把 callback 当作已认证用户请求。

## 4. 重放、幂等与 Operation Policy

- [x] 4.1 将 callback request id 与 Task、scope、claim generation、attempt、operation、目标 SHA 和规范化输入摘要绑定；同绑定重复请求复用事实，冲突绑定拒绝，旧 claim 不得通过新 request id 重放。
- [x] 4.2 保证 callback 认证与当前执行校验先于反向图片缓存、usage 和 `analysis.reverse_image_search` acquire；callback 凭据与 operation grant 不得相互替代。
- [x] 4.3 对 provider 已开始、结果未知、网络重试和重复响应复用既有 usage/grant/`unknown_execution` 协议，不得重复 provider、计量、缓存写入、业务写回或阶段推进。

## 5. 迁移、验证与文档

- [x] 5.1 增加凭据签发/验证、错误 audience/operation/task/claim/target、过期与轮换窗口、空配置和 verifier 异常的单元测试。
- [x] 5.2 增加 ASGI 级未认证大 multipart 测试，以 receive、临时文件和 Task 查询计数证明认证失败发生在 body 读取与业务访问之前。
- [x] 5.3 增加并发重新认领、`claim_generation == 0`、旧 generation、错误 owner、租约过期、取消和终态 callback 测试，断言零进度、零 usage、零 provider 和零写回。
- [x] 5.4 增加跨 scope task id、factory/子服务 scope 错配、客户端伪造 scope/path/grant/attempt/operation 和任务存在性探测测试。
- [x] 5.5 增加视觉目标 SHA 不一致、反向图片任意替换、受控裁剪、request id 改绑、相同请求幂等、provider unknown 及 policy 拒绝测试。
- [ ] 5.6 覆盖 host、Docker 与 Compose Runner/CLI 集成，扫描环境、payload、artifact、日志和缓存，确认只暴露当前任务级 callback 凭据且 executor token 与根 secret 不泄露。
- 5.6 验收缺口：当前会话没有可用 Docker daemon、真实模型凭据或视觉权重，因此 host 夹具和静态边界已验证，Docker/Compose Runner 的真实容器扫描留待 staging/release 环境；本项保持未勾选。
- [x] 5.7 更新内部 callback、Agent Runner、反向图片和部署迁移文档，说明强制认证、secret 轮换、旧任务收束和禁用式回滚；运行严格 OpenSpec、Python、PostgreSQL、Agent/反向图片回归与静态 secret 扫描。
