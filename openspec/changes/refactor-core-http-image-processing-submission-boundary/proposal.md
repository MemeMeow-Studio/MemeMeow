## Why

`api.py` 仍把 `POST /images/processing` 的分页枚举、处理选项规范化、Worker/Repository
复用判断、metadata 读取和逐图错误隔离混在其它图片路由之间。该入口只负责批量提交编排，
可以独立提取并保留现有图片处理控制面。

## What Changes

- 新增公共核心 `backend/image_processing_submission_http.py`，集中承载分页图片处理 Job
  批量提交的 HTTP 编排。
- `api.py` 保留 canonical route、请求模型和 query 声明，通过 callback 注入 Worker、选项
  规范化、Repository、metadata service、environment、processing config 和错误工厂。
- 保持 worker readiness、`reverse_image_policy`/`auto_name` 校验、Job 复用/retry、response
  字段、分页总数和逐图错误隔离语义。
- 增加 route、依赖方向、service 顺序、partial failure 和响应投影契约测试。

## Capabilities

### New Capabilities

- `image-processing-submission-http`: 分页图片处理 Job 批量提交 HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增图片处理提交 HTTP 模块、契约测试和本 change artifacts。实现先在
开源仓库验证并提交，再按精确 SHA 普通 merge 到 Server；不创建 Server 平行实现。
