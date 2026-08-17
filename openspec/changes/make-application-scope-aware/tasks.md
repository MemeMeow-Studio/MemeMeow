## 1. Scope 边界与应用装配

- [x] 1.1 新增 scope 模块，定义可注入 ScopeResolver、LocalScopeResolver、resolve_scope(request) 和稳定解析错误，并补充中文 docstring。
- [x] 1.2 将 FastAPI 入口改为要求显式 resolver 的 create_app；缺失 resolver 时 fail-closed，只有模块级开源入口显式安装 LocalScopeResolver("local")。
- [x] 1.3 实现 scope-bound service factory/facade，复用 Engine、连接池和共享 client，禁止任何共享业务 service 原地切换 scope。
- [x] 1.4 在请求开始时解析一次不可变 scope，并在访问数据库、文件或业务 service 前拒绝空值、非法值和 resolver 异常。

## 2. 请求路由与资源服务

- [x] 2.1 迁移上传、图片库、媒体、元数据和搜索路由，使其仅使用请求 scope 的 metadata/blob/task/search facade，拒绝客户端 scope/user/路径覆盖。
- [x] 2.2 迁移合集及缓存相关路由，使 repository、BlobStore、generation 与请求 scope 一致，并保持 local API 和文件布局兼容。
- [x] 2.3 审核资源不存在、跨 scope 标识和 scope 解析失败的响应与日志，避免泄露其他 scope 的存在性、文件路径或 namespace。
- [x] 2.4 静态扫描生产业务路径，清除固定 local 的 service/environment 事实来源；只保留显式 local adapter、文件布局兼容和测试夹具。

## 3. 持久任务与后台 scope 传播

- [x] 3.1 使任务提交、查询、取消、重试和去重使用 scope-bound facade，服务端写入不可为空的 Task.scope_id，payload 不承担授权 scope。
- [x] 3.2 将任务协调器演进为进程级 scope-aware Worker manager：认领行后按 task scope 创建服务环境，保留 lane slot、背压和多进程去重。
- [x] 3.3 迁移 heartbeat、进度、fencing、重启恢复、失败重试和终态写回；所有副作用验证 task scope、owner、lease 和 claim generation。
- [x] 3.4 迁移视觉、Agent、批次和索引子任务，确保它们继承父任务 scope，handler 不捕获请求 singleton 或 Worker 默认 local。
- [x] 3.5 对缺失或无效 Task.scope_id 建立启动诊断和稳定失败，不从 payload、环境或 local fallback 猜测归属。

## 4. Agent 与现有 Change 集成

- [x] 4.1 让 OpenCodeRunner 和 task handler 以任务 scope 解析 Meme、复核 SHA 并生成受控图片输入；Agent 不接收客户端 scope/user 或任意物理路径。
- [x] 4.2 让视觉匹配、反向图片和结果 callback 从 task control-plane 记录恢复 scope，不依赖用户 request scope 或 Agent 自报值；不在本 change 新增或假定服务间认证。
- [x] 4.3 与反向图片、合集和 Compose executor active changes 做兼容测试：保持各自的审计、ZIP、token 与 HTTP 协议所有权，仅接入 scope 机制。
- [x] 4.4 为宿主部署定义 non-local Agent input provider 的适配与失败行为，未配置时拒绝任务，绝不扩大到跨 scope 文件访问。

## 5. 验证、部署与文档

- [x] 5.1 增加 resolver 注入、显式 local adapter、缺失 resolver fail-closed、伪造 scope 字段和 local API 兼容测试。
- [x] 5.2 增加 PostgreSQL 双 scope 集成测试，覆盖同名 Meme/合集/任务、媒体、文件 namespace、搜索、缓存和跨 scope 资源标识。
- [x] 5.3 增加并发 API 与 Worker 测试，覆盖上传、列表、媒体、搜索、任务认领、重试、过期 claim、子任务和 finalizer，断言没有 scope 污染。
- [x] 5.4 增加视觉、反向图片和 Agent callback 测试，覆盖 task scope 恢复、伪造 payload、路径越界和存在性侧信道。
- [x] 5.5 运行现有能力与 active changes 回归测试，执行生产路径 local 静态扫描、Python 编译、git diff --check 和 OpenSpec 严格校验。
- [x] 5.6 更新架构、API、宿主同步和部署文档，记录 resolver 注入、服务生命周期、任务事实来源、服务间认证责任、non-local Agent 输入、双 scope staging 与回滚流程。

## 6. 装配一致性与 claim 收束加固

- [x] 6.1 实现统一的 scope service 一致性校验，覆盖请求 middleware、`services_for_task`、内部 `for_task` callback 和 Worker claim 后 resolver；校验外层 `ScopeServices` 及所有 scope-bound 子服务。
- [x] 6.2 修改 scope 装配失败收束接口，传递完整 claim 的 task、scope、owner、generation 和租约条件；禁止仅按 task_id 终止任意 `queued/running` 任务。
- [x] 6.3 让 lane slot 释放与 claim 条件更新绑定，影响行数为零时记录 fencing rejection，不能释放其他 Worker 的 slot 或覆盖其状态。
- [x] 6.4 增加 factory 返回错误 scope、子服务 scope 不一致、装配异常延迟到重新认领之后，以及有效 claim 收束成功的并发回归测试。
