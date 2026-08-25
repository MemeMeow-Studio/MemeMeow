## Purpose

为公共核心和 Server 适配器提供清晰、可观测且兼容的 HTTP 应用装配与生命周期边界，使启动失败、请求授权和资源关闭都在既定安全语义内收束。

## ADDED Requirements

### Requirement: 应用装配必须保留兼容入口和路由合同

公共和 Server 应用 MUST 通过显式可信 scope resolver 装配，且模块级 `api.app`、`server_api.app` 与公开 `create_app` 入口 MUST 继续可导入。生成的应用 MUST 保留既有路由 path、method、注册顺序、schema 可见性、异常处理器和 middleware 语义；扩展只能在明确注册阶段追加自己的路由和 hook。

#### Scenario: 公共应用工厂装配

- **WHEN** 调用方使用合法 resolver 调用公共 `create_app`
- **THEN** 返回的 FastAPI 应用包含与模块级入口相同顺序的公共路由、异常处理器和 scope middleware，并把 resolver 绑定到应用状态

#### Scenario: 缺少或非法 resolver

- **WHEN** 调用方未提供 resolver、提供不可调用对象或把 local resolver 绑定到非 local scope
- **THEN** 工厂立即 fail-closed 抛出稳定 scope 装配错误，且不创建数据库、文件、Worker 或外部客户端

#### Scenario: Server 兼容装配

- **WHEN** 调用方通过 `server_api.create_app` 创建 Server 应用
- **THEN** Server preflight、可信 client IP/body guard、认证/配额扩展、workspace provider 和公共路由按既有顺序装配，且旧 `server_api` 导出仍指向 canonical 实现

### Requirement: 生命周期必须按阶段装配可信资源并保持 scope/auth/quota 边界

应用生命周期 MUST 先加载并验证 Settings、数据库 schema 和 policy/callback/workspace 边界，再创建 OpenCode、视觉客户端、scope service factory 和 Worker。请求 scope MUST 只由可信 resolver 绑定；Server 的认证、Origin/CSRF、quota、callback 预检和 body 上限 MUST 继续在对应 middleware/extension 边界执行，不得通过 local fallback 或客户端字段绕过。

#### Scenario: local 与宿主 scope 启动

- **WHEN** local 应用或自定义宿主 factory 进入 lifespan
- **THEN** local 应用执行既有 flat storage preflight 并装配 local services，宿主应用不创建/探测 local namespace，而是只校验 factory 协议并按持久 Task scope 恢复服务

#### Scenario: 启动门禁失败

- **WHEN** Settings、schema、policy、callback、Server preflight 或 factory 协议不满足生产约束
- **THEN** lifespan 在启动 Worker/外部任务前抛出稳定错误，不放行业务请求，不回退旧存储或匿名 scope，并释放已创建的临时资源

#### Scenario: callback 和 workspace 绑定

- **WHEN** 后台任务需要访问 Agent callback 或 workspace
- **THEN** callback 凭据、task claim、scope、workspace selector 和输入 SHA 继续由服务端事实绑定；缺失/不一致时任务失败并返回既有稳定错误码，不暴露路径、密钥或跨 scope 内容

### Requirement: 关闭必须收束后台副作用和资源

应用退出时 MUST 以既有依赖顺序停止扩展、OpenCode、图片 Worker、scope factory、任务 Worker、共享线程池和数据库 Engine；关闭阶段不得把已完成副作用伪装成可重放状态，也不得留下可继续处理的后台线程。重复进入新的 lifespan MUST 不复用上一轮默认 factory 的可变资源。

#### Scenario: 正常关闭

- **WHEN** lifespan 正常退出
- **THEN** 已启动的扩展按逆序收到 shutdown，后台 Worker/线程池在数据库 Engine dispose 前结束，且默认 factory 不残留在下一轮应用 state

#### Scenario: 启动或关闭期间出现异常

- **WHEN** 扩展、Worker 或资源关闭过程出现异常
- **THEN** 已创建的后续资源仍按边界尝试收束，原始错误保持可诊断且不会通过降级到匿名/local 资源隐藏
