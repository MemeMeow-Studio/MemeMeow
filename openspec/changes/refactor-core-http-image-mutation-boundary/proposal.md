## Why

`api.py` 仍把图片重命名与删除的请求校验、文件安全边界、metadata 持久化和 operation
policy 收束混在上传与图片查询入口之间。两个按稳定 `meme_id` 操作的变更入口具有清晰且
可独立审查的副作用边界，现在提取可以继续缩小 HTTP 入口职责而不引入新的公共协议。

## What Changes

- 新增公共核心 `backend/image_mutation_http.py`，集中承载 `/images/rename` 与
  `/images/delete` 的 HTTP 编排、错误投影和副作用顺序。
- `api.py` 保留请求模型、canonical route decorator 和旧 handler 名称，通过 callback 注入
  当前 scope metadata、文件名规范化、operation policy、检索失效和错误工厂。
- 保持稳定 `meme_id` 目标、文件名/存储键校验、目标冲突错误、删除 operation grant
  acquire/commit/release、metadata 更新和成功响应字段。
- 增加 route、依赖方向、文件名边界、operation 顺序、失败收束和响应投影契约测试。

## Capabilities

### New Capabilities

- `image-mutation-http`: 图片重命名与删除 HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增图片变更 HTTP 模块、契约测试和本 change artifacts。实现先在
开源仓库验证并提交，再按精确 SHA 普通 merge 到 Server；不创建 Server 平行实现，不修改
上传、图片处理、数据库 schema 或前端。
