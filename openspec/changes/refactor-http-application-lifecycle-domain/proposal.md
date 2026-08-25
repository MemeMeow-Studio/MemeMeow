## Why

`api.py` 的路由、请求 middleware、Settings/数据库装配、scope factory、Worker、OpenCode、视觉客户端、callback、workspace 和 shutdown 仍集中在同一入口文件，导致公共核心与 Server 适配层无法独立核验生命周期边界。现在已有多个 HTTP/持久化职责域完成拆分，需要把应用装配作为最后的跨域边界收敛，降低循环依赖和启动失败时的越权/资源泄漏风险。

## What Changes

- 新增公共 HTTP 应用装配模块，集中校验 resolver、复制路由模板、安装 middleware/异常处理、注册扩展并保存不可变宿主依赖；`api.create_app` 继续作为兼容入口。
- 新增公共生命周期模块，将 Settings、数据库资源、operation policy、callback、workspace、OpenCode、视觉客户端、scope service factory、Worker 和 shutdown 的阶段化职责从 `api.py` 抽出。
- 保持公共模块级 `api.app`、`api.lifespan`、旧 helper/import 和全部路由 path/method/顺序、scope/auth/quota/callback/worker 语义。
- 将 Server 的 `create_app`、preflight、可信 client IP/body guard、middleware、ServerSecurityExtension 和扩展注册收敛到 Server canonical 装配模块；`server_api.py` 继续兼容导出并保留模块级 `app`。
- 增加应用装配/生命周期契约测试、依赖方向测试、启动失败和关闭顺序测试；在 change 内记录实现 SHA、验证结果、同步状态和回滚步骤。

## Capabilities

### New Capabilities

- `http-application-lifecycle`: 描述公共与 Server HTTP 应用装配、生命周期阶段、兼容入口和 fail-closed 运行约束。

### Modified Capabilities

无。现有业务 HTTP 协议、错误码、数据库 schema 和公网安全要求不变。

## Impact

影响公共仓库 `/home/infstellar/vscode/MemeMeow` 的 `api.py` 与新增 `backend/application.py`、`backend/application_lifecycle.py`，以及 Server 的 `api.py`、`server_api.py` 与新增 `server/application.py`。测试覆盖 `tests/test_api.py`、scope/回调/安全/运行时隔离测试及新增装配契约测试；不新增依赖、不修改数据库迁移、前端或部署配置。公共核心先形成精确 commit，Server 仅在批准的本地同步流程中通过普通 merge 合入；用户现有脏文件与 active change 不属于本 change 的写入目标。
