## Why

`backend/database.py` 仍直接实现视觉向量校验和 `VisualEmbeddingRepository`，与任务、反向图片、文件存储和资源装配共处于兼容 facade 中，导致视觉持久化边界难以单独审查。前置持久化切片已经建立 repository 子包；现在提取视觉向量实现可以降低入口职责密度，同时保持现有 scope、向量身份、错误码和匹配资格事实不变。

## What Changes

- 新增 `backend/persistence/repositories/visual_embeddings.py`，承载视觉向量校验和 `VisualEmbeddingRepository` 的唯一实现。
- 让 `backend.database` 删除重复实现并显式 re-export 同一个 `VisualEmbeddingRepository` 与 `validate_visual_vector` 对象，保持旧导入路径和类/函数身份。
- 在持久化 repository 包入口提供 canonical export；`DataEnvironment` 继续通过既有 facade lazy assembly 使用共享 Session 和绑定 scope。
- 增加视觉向量 repository 的实现唯一来源、依赖方向、scope、模型身份、SHA、候选资格、稳定排序和兼容 facade 契约/回归测试，并记录 validation。
- 不修改视觉 HTTP/推理服务、任务、反向图片、callback、BlobStore、StorageCoordinator、ORM schema/migration、frontend 或其它 active change。

## Capabilities

### New Capabilities

- `persistence-visual-embedding-repository`: 视觉向量校验、scope 绑定持久化和兼容导出的 repository 边界。

### Modified Capabilities

无。本 change 只提取既有实现，不改变公开业务要求。

## Impact

影响公共核心 `backend/database.py`、新增 `backend/persistence/repositories/visual_embeddings.py` 及包入口、视觉持久化契约/回归测试和本 change artifacts。`backend.visual` 继续通过旧路径调用校验函数；`DataEnvironment` 的单 Session、scope 绑定和资源装配不变。实现先在 `/home/infstellar/vscode/MemeMeow` 完成并提交，再按精确来源 SHA 从本地 fetch 到 Server，以普通 `--no-ff` merge 引入；不访问 `upstream` 或 push。
