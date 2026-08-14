## MODIFIED Requirements

### Requirement: 请求和响应字段必须稳定
规范检索请求 MUST 包含非空 `query`，可选 `n_results`（整数，范围 1 到 30）和 `llm_enhance`（默认 `false`）。成功响应 MUST 继续使用 `{ "results": string[] }` 结构，其中每个字符串 MUST 是 `/media/{meme_id}` 形式的受控媒体 URL；错误响应 MUST 使用统一错误标识和人类可读消息。

#### Scenario: 参数越界
- **WHEN** `n_results` 小于 1、大于 30 或不是整数
- **THEN** 系统返回 `400`，错误标识为 `invalid_request`

#### Scenario: 成功响应
- **WHEN** 搜索成功完成
- **THEN** 响应状态为 `200`，`results` 是按规格排序的 `/media/{meme_id}` 字符串数组

### Requirement: 图片响应不得暴露服务器路径
所有返回给客户端的图片地址 MUST 是同一服务提供的 `/media/{meme_id}` 受控媒体 URL，不得包含服务端绝对路径、storage key、scope、密钥或内部缓存标识。媒体接口 MUST 从可信请求上下文取得 scope，并仅在该 scope 中解析 `meme_id`。

#### Scenario: 返回本地图片
- **WHEN** 检索命中当前 scope 的本地图片
- **THEN** `results` 中的值使用 `/media/{meme_id}` 形式且可通过受控媒体接口访问

#### Scenario: 使用旧路径式媒体 URL
- **WHEN** 客户端请求旧的 `/media/{file_path}` 且该值不是合法 `meme_id`
- **THEN** 系统返回明确的 `404` 或迁移错误，不按客户端路径读取文件
