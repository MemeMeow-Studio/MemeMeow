## Why

`api.py` 仍把合集 ZIP 的 multipart 解析、包安全预检、scope-bound 存储、operation policy、成员关系和异步处理投递揉在单一路由中，导致公共 HTTP 入口继续承担过多副作用编排。合集 CRUD 已经拥有独立边界，现在可以把导入作为单独且可回滚的公共路由切片，降低入口职责密度并保留现有协议。

## What Changes

- 新增公共核心 `backend/collection_import_http.py`，承载 `/collections/import` 的请求解析、ZIP 预检、逐成员导入和任务投递编排。
- `api.py` 保留 canonical route decorator、旧 `import_collection` handler 名称和 `_collection_package_error` 兼容 helper，通过 callback 注入当前 scope、metadata、operation policy、处理 Worker 和错误工厂。
- 保持 multipart 文件字段、压缩/解压/成员/图片资源上限，manifest 成员 SHA-256 与路径校验、同名复用及冲突重命名规则。
- 保持合集名称冲突、跨 scope 资源隔离、operation acquire/commit/release、durable 文件与 Meme 写入顺序、视觉/处理任务响应字段和部分成功投影。
- 明确不迁移合集 CRUD、Server `/collections/{collection_id}/export`，不在新模块注册重复路由，也不引入入口反向依赖。
- 增加路由唯一性、依赖方向、资源边界、成员校验、scope、policy 副作用顺序和逐项响应契约测试。

## Capabilities

### New Capabilities

- `collection-import-http`: 合集 ZIP 导入 HTTP 边界及其安全、权限、资源和逐项结果契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增合集导入 HTTP 模块、合集导入/API/scope/security 契约测试和本 change artifacts。实现先在 `/home/infstellar/vscode/MemeMeow` 验证并提交，再由 Server 从本地精确 SHA fetch 后普通 `--no-ff` merge；不访问 `upstream`、不 push Server，不修改数据库 schema、前端、Server 导出边界或其它 active change 脏文件。
