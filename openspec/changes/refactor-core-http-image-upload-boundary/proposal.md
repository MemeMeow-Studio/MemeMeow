## Why

`api.py` 仍把 `/images/upload` 的 multipart 解析、请求与单文件字节预算、图片预检、幂等
查找、operation grant、durable 写入和处理任务提交揉在一个入口中。上传是图片生命周期中
最重的剩余公共 HTTP 域，先抽取它可以显著降低入口职责密度，同时保留既有 scope、配额和
逐文件部分成功语义。

## What Changes

- 新增 `backend/image_upload_http.py`，集中承载图片上传 multipart 边界、逐文件编排、幂等
  结果和处理任务投递。
- `api.py` 保留 `POST /images/upload` 的 canonical route、旧 handler 名称及 helper 兼容
  re-export，通过显式 callback 注入当前 scope service、错误工厂、文件名/图片校验、操作
  policy、任务服务和处理 worker。
- 保持最多 20 个文件、可选总请求字节预算、单文件限制、未知表单字段拒绝、幂等上传、批量
  部分成功、响应 key、错误 code 与 operation acquire/commit/release 收束顺序。
- 增加独立模块依赖、route metadata、multipart 字节边界、scope/policy fail-closed、文件
  与任务副作用顺序的契约测试。

## Capabilities

### New Capabilities

- `image-upload-http`: 图片上传 HTTP 边界及其 multipart、durable 与处理任务契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增图片上传 HTTP 模块、上传边界和 API 契约测试及本 change artifacts。
不修改数据库 schema、前端、合集导入、图片处理实现或公开 URL/method/status/response contract；
实现先在 MemeMeow 验证并提交，再按精确 SHA 同步到 Server。
