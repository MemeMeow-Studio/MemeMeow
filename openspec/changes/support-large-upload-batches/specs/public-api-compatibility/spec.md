## ADDED Requirements

### Requirement: 上传接口契约必须声明请求边界和可恢复错误

项目 API 文档、OpenAPI 和前端调用 MUST 使用 `/images/upload` 的 `files`、`reverse_image_policy` 和 `auto_name` 字段，并明确最多 20 个文件、可选总请求字节预算、逐文件结果及稳定错误标识。超过文件数、超出可选预算、同名冲突和事实不一致 MUST 使用可区分的 HTTP 状态或错误码。

#### Scenario: 客户端按公开配置调用

- **WHEN** 客户端读取 `/config` 后按公开文件数和预算构造上传请求
- **THEN** 请求使用规范 `/images/upload` multipart 入口
- **AND** 响应保持 `{ "batch_id": ..., "results": [...] }` 及逐文件结果结构

#### Scenario: 超过请求边界

- **WHEN** 客户端越过公开的文件数或总字节边界
- **THEN** 响应包含稳定错误标识和可读消息
- **AND** 不报告任何未完成的文件为成功

#### Scenario: 幂等成功响应

- **WHEN** 服务端认领同一 durable 上传事实
- **THEN** 结果包含既有 `meme_id`、文件名和当前可见处理状态
- **AND** 客户端无需知道持久 upload session 或 batch 实体
