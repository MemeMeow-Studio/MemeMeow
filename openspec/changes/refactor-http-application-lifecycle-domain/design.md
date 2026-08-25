## Context

当前公共 `api.py` 同时保存路由模板、请求 middleware、约 860 行 lifespan 及 `create_app`；Server `server_api.py` 又把生产 preflight、可信来源/body guard、认证/配额扩展和外层 lifespan 包装放在同一个模块。已有领域模块通过单向依赖从入口抽出，入口仍需要兼容导出和模块级 `app`。本 change 只重排职责，不改变路由协议、数据库模型或公网安全规则。

## Goals / Non-Goals

**Goals:**

- 建立公共 `backend/application.py` 装配边界和 `backend/application_lifecycle.py` 生命周期边界，避免 canonical 模块反向导入 `api.py`。
- 以阶段化 helper 显式表达 Settings、数据库、policy/callback/workspace、OpenCode/视觉客户端、scope factory/Worker 和 shutdown 的 ownership 与顺序。
- 让公共 `api.create_app`、`api.app`、`api.lifespan` 和 Server `server_api` 旧导出保持兼容。
- 在 Server 侧集中 app factory/preflight/middleware/扩展接线，确保公共核心只提供通用生命周期扩展协议。
- 用路由快照、导入方向、启动失败、scope 隔离、callback 绑定和关闭顺序测试证明等价性。

**Non-Goals:**

- 不改变业务 handler、路由 URL/method/status/响应字段、scope/auth/quota/callback 错误码、Worker payload、数据库 schema/migration 或部署配置。
- 不把所有路由从 `api.py` 迁走；本 change 只收敛应用装配和生命周期依赖，既有领域 HTTP 模块继续作为路由实现。
- 不在 Server 中复制公共实现；公共改动必须先在开源仓库提交并按精确 SHA 普通 merge 同步。
- 不触碰用户当前 README、OpenCode、Docker/PM2、fair scheduling、thumbnail、observability 等未提交文件。

## Decisions

### 1. 用两个公共 canonical 模块表达不同 ownership

`backend/application.py` 只负责 FastAPI app 的模板复制、路由/异常/middleware 接线、resolver/policy/provider state 和扩展 route 注册；`backend/application_lifecycle.py` 只负责运行时资源阶段和 shutdown。`api.py` 保留路由定义与兼容别名，并调用两个模块。这样可独立测试“装配前不触碰外部资源”和“lifespan 内按顺序创建资源”。

备选方案是一次性把整份 `api.py` 搬到新模块；该方案会把路由 handler 的私有 helper、任务 handler 闭包和大量公共符号一起迁移，增加循环依赖和 Server merge 冲突，无法形成小而可回滚的边界，因此不采用。

### 2. 生命周期使用不可变 setup/runtime 记录传递 ownership

生命周期模块返回包含 Settings、Engine、DatabaseResources、factory、Worker manager、共享 executor、local services 和已启动扩展的记录。任务 handler 仍由 `api.py` 提供，因为它们直接依赖既有 HTTP 私有 helper；模块通过显式 `register_handlers`、task handler 映射和 `start_services` 回调接入，不从 `api` 反向 import。所有资源写入 `app.state` 的位置集中在生命周期模块，关闭函数接收同一记录并按顺序收束。

### 3. `api.create_app` 仅做兼容参数门禁和 canonical delegate

公共工厂保留 resolver/policy 参数校验和旧导出，但 FastAPI 实例创建、路由模板复制、middleware/exception handler 复制、扩展注册和 state 初始化委托给 `backend.application.create_application`。路由模板仍由 `api.py` 的装饰器生成，避免改变 APIRoute 身份、顺序和已有测试夹具。

### 4. Server 只包装公共生命周期，不拥有公共资源

Server canonical 模块负责安装 `ServerSecurityExtension`、quota extension、Server workspace provider、外层 request boundary middleware 和 guarded lifespan。preflight 在进入公共 lifespan 前执行；认证/配额扩展在公共数据库/factory/Worker 就绪后执行。`server_api.py` 显式 re-export canonical Server 符号并保留 `app = create_app()`。

### 5. 迁移以 route/state 快照和行为回归为门禁

新增测试记录公共/Server route path-method-name-tags/schema 顺序、旧 import 身份、应用装配无副作用、preflight 早于 Worker、scope/local 分支、callback/workspace state、shutdown 顺序以及 canonical 模块没有反向入口导入。现有 HTTP、安全、scope、Worker、OpenCode 和 runtime 测试继续运行；真实 PostgreSQL/Compose 未配置时只记录 skip。

## Risks / Trade-offs

- [Risk] 公共 `api.py` 与 Server `api.py` 存在 quota/安全差异，直接同步可能产生 merge 冲突。→ 先在开源仓库完成只涉及公共模块/公共接线的精确 commit，Server 用普通 `--no-ff` merge 并逐段核验 Server 专属差异。
- [Risk] 任务 handler 闭包依赖 lifespan 局部变量，抽取时可能失去 scope 或 claim 绑定。→ 使用显式 runtime 记录和回调参数，保留 payload/handler 逻辑不变，并运行真实 Server lifespan fixture。
- [Risk] 扩展注册或 middleware 重建顺序变化会影响认证/限流。→ 对 route 顺序、`user_middleware` 顺序、extension hook 记录和 Server preflight-before-worker 添加契约测试。
- [Risk] 启动中途失败留下 engine/executor。→ 生命周期记录只在资源成功创建后接管 ownership；失败路径使用统一收束函数，测试注入 schema/policy/factory 失败并断言 dispose/shutdown。
- [Risk] 旧 import 指向包装函数而非原对象。→ 入口使用显式别名 re-export，测试 `is` 身份和 canonical 模块无 `api`/`server_api` 反向导入。

## Migration Plan

1. 在公共仓库确认工作区干净，新增 canonical 模块、公共 `api.py` delegate 和公共装配/生命周期测试；运行公共测试与严格静态门禁并提交唯一公共 commit。
2. 记录公共精确 SHA，按用户批准的流程获取对应历史并在 Server `main` 普通 `--no-ff` merge；解决仅限 Server quota/安全差异的适配冲突。
3. 新增 Server canonical 装配模块，令 `server_api.py` 只兼容导出与入口；运行 Server 安全、scope、runtime、HTTP 全量回归。
4. 在本 change 的 validation/sync-record 中记录公共 SHA、Server merge SHA、祖先关系、测试、skip 门禁、剩余风险和回滚命令。

回滚：先停止应用，恢复 `api.py`/`server_api.py` 的旧 delegate 接线并删除新增 canonical 模块；若已同步公共 commit，Server 通过反向普通 merge 或恢复到 merge 前 commit 回滚，不修改数据库/运行数据。不得使用 reset、checkout、stash 覆盖用户当前脏文件。

## Open Questions

无。公共同步、Server merge、模块边界和验证门禁已由任务与仓库规则固定。
