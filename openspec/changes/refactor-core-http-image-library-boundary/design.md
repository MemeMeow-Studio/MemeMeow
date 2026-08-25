## Context

图片库公开稳定身份是当前 scope 派生的 `meme_id`。列表需要读取 Meme 分页、文件指纹、
metadata 状态、文本缓存和视觉向量状态，并可附带最新处理 Job 摘要；详情和媒体读取必须
通过 scope-bound metadata service 校验数据库记录与实际文件 SHA/size。客户端不能提交路径、
目录或 scope 选择器。

## Goals / Non-Goals

**Goals:**

- 将 `/images`、`/images/metadata` 和 `/media/{meme_id}` 的只读编排移到不依赖 `api.py` 或
  `server_api` 的公共模块。
- 通过显式 callback 注入 scope services、environment、processing repository、visual identity
  和错误工厂，保持旧 route/handler/query 兼容。
- 保持图片列表字段、分页、metadata 状态、媒体类型和错误 status/code 投影。

**Non-Goals:**

- 不迁移上传、重命名、删除、图片处理 Job、BlobStore、MetadataService 或数据库 schema。
- 不改变 scope middleware、Server adapter、前端或媒体文件权限语义。
- 不接受旧目录、绝对路径、scope/user 或客户端图片归属字段作为兼容输入。

## Decisions

### 1. Query 声明留在入口

FastAPI `Query` 参数和原 route decorator 留在 `api.py`；新模块只接收已校验的 search/page/page_size
和 meme_id，避免 route metadata 与 OpenAPI 漂移。

### 2. 只读依赖通过 callback 注入

新模块不直接构造 ScopeServices 或数据库资源；入口注入当前 scope 的 services/environment，
处理 repository 与 visual identity 读取通过 callback 完成。这样列表能保持现有跨 repository
事实，同时新模块不反向依赖应用装配。

### 3. 文件一致性继续由 metadata service 负责

详情和媒体 handler 只调用 `image_for_meme`，由既有 metadata service 校验 BlobStore 路径、
实际 SHA/size 与数据库记录；handler 只投影稳定 `meme_not_found` 或 metadata 错误。

## Risks / Trade-offs

- [scope 或路径泄露] -> 测试 query selector 拒绝、模块依赖方向和 media 只接收 meme_id。
- [列表状态漂移] -> 测试 metadata/embedding/visual/processing 摘要和分页响应字段。
- [指纹校验被绕过] -> 测试详情/媒体由 metadata service 抛出 mismatch 时的稳定错误投影。

## Migration Plan

1. 在开源仓库新增模块、契约测试和 OpenSpec artifacts，保留 `api.py` 薄 wrapper。
2. 运行图片库/API/scope/security 回归、compileall、strict validate 与 diff check，提交精确实现
   及验证记录 SHA。
3. 按已批准的精确 SHA fetch 并普通 `--no-ff` merge 到 Server，再运行 Server 定向回归。
4. 回滚时恢复 `api.py` 原只读 handler，删除新模块、测试和本 change artifacts，不修改 schema。
