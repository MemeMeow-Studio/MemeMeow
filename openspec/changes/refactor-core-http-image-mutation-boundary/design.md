## Context

当前 `api.py` 的两个图片变更 handler 已具备稳定的输入和副作用顺序：先以当前 scope 的
metadata service 查找 Meme，再进行文件名或 operation policy 校验，最后写入文件/metadata
并使检索失效。重命名和删除共享图片身份边界，但上传、图片处理和只读媒体路由拥有不同的
资源预算或状态机，不应在本 change 中混合迁移。

## Goals / Non-Goals

**Goals:**

- 将重命名、删除的 HTTP 编排提取到不依赖 `api.py` 或 `server_api` 的公共模块。
- 以显式 callback 保留 scope service、错误工厂、文件名规范化、operation policy 和检索
  失效的宿主职责，保持旧 route、模型和 handler 兼容。
- 让单元测试能够独立验证文件目标边界与 operation grant 的收束顺序。

**Non-Goals:**

- 不迁移上传 multipart parser、图片处理 Job、BlobStore、MetadataService、operation policy
  实现、数据库 schema、scope middleware 或前端。
- 不改变文件移动、metadata 事务、quota/计量策略或任何公开状态码和字段。

## Decisions

### 1. Route 和请求模型留在入口

FastAPI route decorator、`RenameRequest`、`DeleteRequest` 以及旧 handler 名称继续留在
`api.py`。新模块只接收 FastAPI 已校验的 payload，避免 route metadata 和 OpenAPI 顺序漂移。
直接删除旧业务实现而保留薄 wrapper；不在新模块注册路由，避免重复 canonical route。

### 2. 宿主依赖通过 callback 注入

metadata service provider、error factory、`_safe_filename`、search invalidation 和 operation
acquire/commit/release 均由入口注入。模块只导入稳定的错误类型、业务 storage key 校验和
operation 常量，不构造 scope/environment 或数据库资源；这比在模块中重新读取 `app.state`
更能保持 scope 装配单向且可测试。

### 3. 保留副作用顺序和 fail-closed 映射

重命名先查找源记录、规范化并校验目标，再调用 metadata rename，成功后才失效检索缓存。
删除先查找记录并 acquire grant，metadata 删除成功后再 commit；只有明确列出的未 durable
错误才 release，commit 失败保留已完成删除事实。这些顺序由契约测试锁定，不把 policy 失败
降级为允许操作。

### 4. 不抽取上传共享 helper

上传仍位于 `api.py`，因为其 multipart 解析、请求级字节预算、幂等上传和 processing job
编排明显不同。此切片只抽取重命名与删除，确保可回滚且不扩大公共边界。

## Risks / Trade-offs

- [callback 参数遗漏导致宿主行为漂移] → 保留旧 handler 形状并增加 API wrapper 参数断言与
  新模块顺序测试。
- [文件名校验被简化] → 复用现有 `_safe_filename` 与 `validate_business_storage_key`，并
  覆盖分隔符、控制字符、扩展名和冲突目标测试。
- [删除后 policy commit 失败被误报为失败] → 明确保持 durable 删除成功响应，并测试不调用
  release；未知 policy 状态交由既有恢复边界处理。

## Migration Plan

1. 在开源仓库新增模块、契约测试和 OpenSpec artifacts，保留 `api.py` 薄 wrapper。
2. 运行图片/API/scope/security 回归、compileall、strict validate 与 diff check，提交实现
   及独立验证记录 SHA。
3. 按用户已批准的精确 SHA 从本地开源仓库 fetch，并在 Server 普通 `--no-ff` merge；Server
   不访问 upstream、不 push，随后运行适配层定向回归。
4. 回滚时恢复 `api.py` 两个原 handler 并删除新模块、契约测试和本 change artifacts，不改
   schema 或上传实现。
