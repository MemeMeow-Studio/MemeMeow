## Why

图片解码和合集 ZIP 预检目前只检查字节数、格式和部分路径规则，恶意输入仍可通过超大像素图、动画帧或高压缩比消耗过多 CPU 与内存。公网入口需要在任何 durable 写入、图片处理任务或模型调用前执行统一且可终止的资源边界检查。

## What Changes

- 新增共享图片资源预检，限制单帧像素、动画帧数、累计帧像素，并为每个文件设置可终止的 10 秒预检截止时间。
- `/images/upload` 在幂等查找和 durable 写入前统一执行图片资源预检，稳定返回逐文件资源错误。
- 合集 ZIP 导入限制压缩包原始字节、成员数量、单成员/总解压大小和压缩放大比，拒绝 ZIP64、嵌套归档、目录、链接及其它特殊文件。
- 合集预检同时校验 ZIP 中央目录声明大小、实际读取大小、manifest 声明和内容指纹；导出读取使用受控文件描述符，避免路径竞态跟随符号链接。
- 补充恶意图片、动画、ZIP 放大、ZIP64、特殊条目、声明不一致、路径竞态和失败清理测试。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `image-ingestion`: 上传图片必须在入库前通过统一的像素、帧数和预检时间边界。
- `meme-collections`: 合集 ZIP 导入导出必须遵守压缩包资源和条目类型边界，并在预检失败时不产生业务副作用。

## Impact

影响 `backend/image_safety.py`、`backend/collection_packages.py`、`api.py` 以及相关 Python 测试和本 change 的 delta specs。不会新增数据库表、公开凭据或跨仓库同步；已有合法图片和 v1 manifest 在边界内保持兼容。
