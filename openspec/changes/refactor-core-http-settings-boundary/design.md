## Context

当前 `api.py:307-312` 定义设置并发请求模型，`api.py:2043-2114` 还同时负责状态投影、
管理 token 解析与校验、dotenv 写入编排以及 canonical/legacy 路由注册。状态数据本身由
`backend/config.py:333-427` 生成，受保护的原子 dotenv 更新由
`backend/config.py:457-522` 提供；这些配置领域能力已经是公共核心能力，不属于服务器
适配层。`api.py:1507-1517` 先创建路由模板，`api.py:3479-3505` 再复制模板路由给
宿主自定义应用，因此新路由必须进入同一个模板装配路径。

现有测试只对请求模型和 `/config` 的部分脱敏行为有直接覆盖，例如
`tests/test_api.py:21-63`、`tests/test_api.py:153-165` 和
`tests/test_agent_runtime.py:187-217`；Settings HTTP 的方法/path 可见性、token
优先级、保存错误和 legacy 入口缺少一张高信号契约表。提案的动机见
`proposal.md`，行为边界以 `specs/backend-settings/spec.md` 为准。

## Goals / Non-Goals

**Goals:**

- 把 Settings HTTP 域集中到一个不依赖 `api.py` 的公共核心 `backend/settings_http.py`，
  让模型、投影、授权、写入编排和路由注册具有单一职责边界。
- 保持路由表、OpenAPI 可见性、请求别名、严格校验、状态响应字段、错误 code/status、
  Header/Bearer 兼容和环境覆盖 fail-closed 语义逐项不变。
- 让模块级 `app` 与 `create_app()` 生成的宿主应用都接收到同一组 Settings 路由，且
  不改变其它路由的顺序、生命周期或 scope 解析。
- 通过契约、旧 import/path 回归和静态依赖检查，证明该切片可以单独回退。

**Non-Goals:**

- 不移动或重构 `Settings` 模型本身，不修改 `backend/config.py` 的校验和原子写入算法。
- 不提取 `/config` 通用运行状态、search、lifespan、scope middleware、其它业务路由或
  server adapter；`config_status` 继续留在 `api.py`。
- 不改变数据库 schema、migration、前端、URL/method/status/response keys 或引入依赖。

## Decisions

### 1. 使用独立的 Settings HTTP 模块和单向依赖

新增 `backend/settings_http.py`，模块级文件 docstring 说明它位于公共核心 HTTP 装配
边界，负责 Settings 的 HTTP 输入和输出。它直接依赖 FastAPI/Pydantic、
`backend.config` 和必要的公共 scope 类型；它 MUST NOT `import api` 或从 `api.py`
读取 helper。错误异常在该模块内构造为现有 `{error, message}` detail，继续交给应用已有
的全局 HTTPException/RequestValidationError handler 统一输出。

依赖方向固定为：

```text
backend.settings_http -> backend.config / backend.scope / FastAPI
api.py -> backend.settings_http
create_app/template -> settings_router
```

这样 `api.py` 只依赖设置路由，而设置模块不会回头依赖应用入口；生命周期、数据库和
其它路由仍由 `api.py` 所有。`ScopeServices` 只用于保持当前请求级 search cache 的
选择语义，不能从设置模块反向获取 `/config` 或其它 API helper。

### 2. 用 APIRouter 保持六个 method/path 注册和模板顺序

设置模块导出一个专用 `APIRouter`，按现有 decorator 的注册顺序定义：

1. `GET /settings`（隐藏）和 `GET /backend/settings`（公开）；
2. `PATCH /settings`（隐藏）和 `PATCH /backend/settings`（公开）；
3. `POST /backend/settings/concurrency`（公开）和 `POST /backend/settings`（隐藏）。

`api.py` 在 `_route_template` 已创建且位于原 Settings 路由注册位置时直接展开
`settings_router.routes`。FastAPI 当前版本的 `include_router()` 会把子路由保留为延迟的
`_IncludedRouter`，而项目的 `create_app()` 会复制模板的 `router.routes` 并且既有测试和
宿主代码读取其中的 `APIRoute.path`；直接展开既保留 APIRouter 的定义能力，也维持原路由
表的具体对象形态和顺序。模块中保留原 handler 名称；`api.py` 显式导入并 re-export
请求模型、公开 handler 及既有私有 Settings helper，减少旧 Python 调用方的 import
断裂。不会通过 prefix、重命名 operation 或额外 POST `/settings` 获得“更整洁”的表，
因为这些都会改变现有 HTTP 兼容面。

### 3. 原样迁移状态投影而非重新定义响应 schema

`_backend_settings_status` 迁移后继续读取 `request.app.state.settings`、当前请求
`ScopeServices.search`（无请求服务时沿用 `app.state.search_engine`）以及
`app.state.opencode.runtime_probe()`；仍调用 `Settings.backend_status()`，并只追加既有
的 `readonly/read_only.visual_available` 布尔字段。不得把 `/config` 的状态拼入 Settings
投影，也不得暴露完整 runtime probe、路径或密钥。

保存成功后继续在该投影上追加 `saved: true`、基于当前有效值计算的
`restart_required` 和提交值 `pending.opencode_concurrency`；当前进程不热更新 Settings
或 worker。实现先以现有响应 fixture/路由快照建立基线，再迁移代码，避免因为字段排序、
重复别名或 `None` 处理变化产生隐性兼容破坏。

### 4. 集中 token 解析并保留 fail-closed 顺序

设置模块提供一个私有 token 提取函数，严格复用现有优先级：先取
`X-Settings-Admin-Token`，再取 `X-MemeMeow-Settings-Token`，两者都没有有效值时才
解析大小写不敏感的 `Authorization` 中 `Bearer ` 后缀并去除两端空白。授权函数从
`request.app.state.settings.settings_admin_token` 读取配置；缺失配置、缺失 token 或
安全比较失败统一抛出 `403/settings_forbidden`。

环境覆盖检查仍在任何 dotenv 写入前执行，并按当前 `os.environ` 是否存在
`MEMEMEOW_OPENCODE_CONCURRENCY` 判定；该变量即使是空字符串也不能被页面写入绕过。
有效值继续传给 `update_dotenv_concurrency(settings.dotenv_path, value,
backpressure=settings.agent_backpressure)`。`ValueError` 与 `OSError` 的映射保持
`400/settings_update_invalid` 和 `409/settings_update_failed`，不把内部异常原文作为
成功响应或新的有效配置。

### 5. 兼容性由契约测试而不是重复实现保证

新增设置专属测试模块（或在现有 API 测试中添加同等隔离夹具）覆盖：

- `api.app.routes` 的 path、method、handler name、tags 和 `include_in_schema` 快照；
- 四个 JSON 输入别名及严格整数/未知字段矩阵；
- 两个 token Header、Bearer 大小写、Header 优先级、缺失/错误 token；
- canonical/legacy 成功和 405 路径、错误 code/status、完整 response key 集合；
- 环境覆盖时 dotenv 字节内容/mtime 不变，成功写入只产生 pending/restart 状态；
- `from api import ConcurrencyUpdateRequest` 以及现有 handler import 仍可解析，且新模块
  的源码依赖检查确认没有 `api` 反向导入。

测试不需要连接真实数据库：状态投影使用最小 app state doubles，dotenv 使用
`tmp_path`，端到端路由只复用项目既有 TestClient fixture。已有 `/config`、生命周期和
其它业务测试继续作为回归门禁。

### 6. 不引入新的抽象层或第三方依赖

APIRouter 只是 FastAPI 已有的注册容器；不新增 generic settings service、repository、
schema/migration 或适配器接口。保持 `backend.config` 作为配置领域的唯一实现，新的
HTTP 模块只编排 HTTP 边界，符合首个切片的最小范围和可回滚要求。

## Risks / Trade-offs

- [Risk] APIRouter 的 decorator 顺序或 OpenAPI 隐藏标志发生变化，导致旧前端/宿主看到
  不同路由表。→ 在迁移前记录 `api.app.routes` 精确快照，迁移后逐项对比 path、method、
  name、tags 和 schema 可见性，并用 canonical/legacy 请求回归。
- [Risk] 新模块为避免循环依赖而自行重写状态投影或错误映射，遗漏脱敏字段。→ 保留
  `Settings.backend_status()` 和 `update_dotenv_concurrency()` 作为唯一领域实现，测试
  成功/失败 response key 和敏感字段禁带。
- [Risk] token 提取顺序或空值语义改变，造成未授权写入或合法旧客户端失效。→ 使用
  Header/Bearer 组合矩阵及环境覆盖 fail-closed 测试，授权失败统一断言 403/code。
- [Risk] `api.py` 兼容 re-export 过少，外部或领域测试的旧 import 断裂。→ 显式保留
  `ConcurrencyUpdateRequest`、三个 route handler 和 Settings 私有 helper 的 import
  回归；后续切片再按使用证据收窄兼容面。
- [Risk] 新增模块被误同步到服务器仓库或与其未提交 change 混淆。→ 本 change 只写
  `/home/infstellar/vscode/MemeMeow/openspec/changes/refactor-core-http-settings-boundary/`
  规划 artifacts；实现前不触碰 MemeMeowServer。

## Migration Plan

1. 在实现开始前保存当前开源工作区状态和 Settings 路由/响应契约快照；确认目标文件无
   重叠未提交修改。
2. 新建 `backend/settings_http.py`，迁移模型、投影、授权、更新编排和 router；在
   `api.py` 的模板注册点接入 router，同时添加显式兼容 re-export，删除原 Settings
   HTTP 定义但保留 `/config`。
3. 先运行设置专属契约、旧 import/path 回归，再运行现有 API、配置、scope 和安全测试；
   若任何方法、status、key、错误 code 或脱敏边界变化，停止并修正，不迁移其它域。
4. 运行 `openspec validate refactor-core-http-settings-boundary --strict` 及必要的
   静态依赖检查。该 change 无数据库或配置迁移，回滚只需恢复 `api.py` 的旧注册和
   删除新模块，`.env` 与运行数据不受影响。

## Open Questions

无。模块命名、路由接入位置、兼容导出范围和测试门禁均已由本设计固定，后续实现不应
借机扩大到 search、lifespan 或 server adapter。
