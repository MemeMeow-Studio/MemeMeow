## Context

合集路由当前与 ZIP 导入、导出以及图片处理共存于 `api.py`。合集 repository 已经提供 scope-bound 的 CRUD、成员分页和幂等批量操作；HTTP 拆分只能搬运编排，不能改变数据库事务、成员身份或 Server 导出覆盖顺序。

## Goals / Non-Goals

**Goals:**

- 将合集列表、创建、详情、重命名、删除和成员增删移到不依赖入口模块的公共 HTTP 边界。
- 由入口保留 FastAPI decorator、请求 DTO、query 校验和兼容函数名，并显式注入 scope environment、metadata service 和错误工厂。
- 让 Server 继续能够覆盖 `/collections/{collection_id}/export`，且公共 `/collections/import` 不被新模块触碰。

**Non-Goals:**

- 不迁移 ZIP manifest、导入预检、导出归档、Server quota/资源限制或 `server/collection_export.py`。
- 不修改 `CollectionRepository`、ORM schema、事务边界、scope resolver、图片存储或前端。
- 不新增客户端 scope/user/task 选择能力，也不改变现有 response/error code。

## Decisions

### 1. 保留入口 route metadata 和 DTO

`api.py` 继续声明七个 canonical route、`CollectionRequest`、`CollectionItemsRequest` 和分页
`Query`。新模块只接收已经由 FastAPI 校验的参数；这样可以避免 OpenAPI、路由顺序和
`include_in_schema` 事实漂移。把 decorator 一并搬到模块会使 Server 的导出替换逻辑更难验证，收益不足。

### 2. 通过 callback 注入当前 scope 资源

环境、metadata service 和错误工厂由 `api.py` 提供。新模块不构造数据库或 scope facade，也不读取
request state 里的隐式全局对象；所有 repository 访问都在当前请求的 environment context 内完成。
详情的文件状态由注入的 metadata service 根据 repository 返回的 Meme 解析，保持现有文件指纹和
scope 绑定责任。

### 3. 将兼容 helper 一并归属边界但保留旧入口

合集摘要和 DatabaseError 到 HTTP 的映射由新模块实现并导出；`api.py` 保留同名兼容 wrapper/alias，
其它旧调用无需改动。新模块不导入入口，因此依赖方向为入口 -> HTTP 边界 -> database/metadata
协议。

### 4. 明确排除导入和导出

导入仍依赖图片上传、operation policy、处理任务和逐项结果，导出由 Server 负责受限进程、临时目录
和 response headers。新 CRUD 模块不注册或调用这两个路径，避免公共实现绕过 Server 安全适配或出现
重复 export route。

## Risks / Trade-offs

- [详情 metadata 读取异常被错误改变] → 保持旧 handler 的异常边界，只把 metadata service 作为 callback 注入，不新增宽泛 fallback；契约测试覆盖正常状态和 repository 错误。
- [scope/query 校验顺序漂移] → 新模块保留现有列表/详情 unknown query 检查，并在注入 repository 前执行；路由级安全回归继续覆盖其它操作。
- [Server 导出覆盖失败] → 新模块不包含导出 decorator，验证公共 route 数量和 Server export route endpoint/order。
- [公共模块反向依赖入口] → 静态 AST 测试拒绝 `api`/`server_api` import。

## Migration Plan

1. 在开源仓库新增 `backend/collection_http.py`、契约测试和本 change artifacts，并让 `api.py` 使用薄 wrapper。
2. 运行合集/API/scope/security 相关测试、完整开源回归、compileall、strict validate 和 diff check，提交实现及独立验证记录。
3. 经用户审核后，Server 从本地开源历史 fetch 精确 SHA，普通 `--no-ff` merge，并运行 Server 定向回归；不 push Server。
4. 回滚时恢复 `api.py` 原合集 CRUD/详情/成员实现并删除新模块及其契约测试；导入、导出和数据库 schema 无需回滚。
