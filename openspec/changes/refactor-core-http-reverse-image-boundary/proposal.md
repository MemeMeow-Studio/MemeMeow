## Why

`api.py` 仍把内部反向图片 callback 的 multipart 读取、callback claim 校验、目标图片
校验、scope service 装配和错误投影混在应用入口中。该边界与视觉 callback 相互独立，
可以单独提取以缩小安全审查范围。

## What Changes

- 新增公共核心 `backend/reverse_image_http.py`，集中承载反向图片 callback handler 编排。
- `api.py` 保留 canonical route、handler 名称和 FastAPI 表单声明，通过 callback 注入
  binding、registration、database、scope service 与错误工厂。
- 保持 multipart body 上限、task claim/attempt/scope/target SHA 校验、受控裁剪、
  request_id/input_digest 绑定、供应商调用顺序及稳定错误码。
- 增加 route、依赖方向、伪造图片/旧 claim 拒绝、绑定冲突和 service 转发契约测试。

## Capabilities

### New Capabilities

- `reverse-image-callback-http`: 内部反向图片检索 callback HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增 callback HTTP 模块、契约测试和本 change artifacts。实现先在
开源仓库验证并提交，再按精确 SHA 普通 merge 到 Server；不创建 Server 平行实现。
