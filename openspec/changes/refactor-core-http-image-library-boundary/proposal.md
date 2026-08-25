## Why

`api.py` 仍把图片库列表、元数据详情和媒体文件读取的 HTTP 校验、scope service 调用、
指纹状态投影和错误映射混在上传/处理路由之间。三个只读入口共享稳定的图片身份边界，
可以独立提取以缩小 HTTP 与文件安全审查范围。

## What Changes

- 新增公共核心 `backend/image_library_http.py`，集中承载图片列表、metadata 和 media 的
  只读 HTTP 编排。
- `api.py` 保留 canonical route、旧 handler 名称和 query 参数声明，通过 callback 注入
  scope services、environment、processing repository、visual identity 和错误工厂。
- 保持废弃目录参数拒绝、分页/搜索、metadata 指纹校验、媒体类型推断、scope 派生和稳定错误码。
- 增加 route、依赖方向、列表状态、metadata/meme_id 和媒体错误投影契约测试。

## Capabilities

### New Capabilities

- `image-library-http`: 图片库只读 HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增图片库 HTTP 模块、契约测试和本 change artifacts。实现先在开源
仓库验证并提交，再按精确 SHA 普通 merge 到 Server；不创建 Server 平行实现。
