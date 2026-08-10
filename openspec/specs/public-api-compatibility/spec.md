## Purpose

为 Vue 前端和外部调用方提供唯一、明确且低复杂度的 HTTP 入口，统一检索、媒体访问、任务和错误响应，避免同一能力存在多套互相漂移的接口。

## Requirements

### Requirement: 检索必须只有一个规范入口
系统 MUST 将 `POST /search` 作为唯一规范检索入口，使用 JSON 请求体；不得同时维护语义相同的 GET、旧版别名或第二套版本化路径。旧的非规范调用 MUST 返回明确的迁移错误或由部署层统一重定向。

#### Scenario: 使用规范入口检索
- **WHEN** 客户端向 `POST /search` 提交合法请求
- **THEN** 系统返回统一 JSON 响应，其中包含 `results` 数组

#### Scenario: 使用旧 GET 形式
- **WHEN** 客户端向旧的 `GET /search?q=...` 形式请求
- **THEN** 系统返回明确的 `405` 或迁移错误，不执行重复的检索逻辑

### Requirement: 请求和响应字段必须稳定
规范检索请求 MUST 包含非空 `query`，可选 `n_results`（整数，范围 1 到 30）和 `llm_enhance`（默认 `false`）。成功响应 MUST 使用 `{ "results": string[] }` 结构；错误响应 MUST 使用统一错误标识和人类可读消息。

#### Scenario: 参数越界
- **WHEN** `n_results` 小于 1、大于 30 或不是整数
- **THEN** 系统返回 `400`，错误标识为 `invalid_request`

#### Scenario: 成功响应
- **WHEN** 搜索成功完成
- **THEN** 响应状态为 `200`，`results` 是按规格排序的图片媒体引用数组

### Requirement: 图片响应不得暴露服务器路径
所有返回给客户端的图片地址 MUST 是同一服务提供的受控媒体 URL 或配置的公开 URL，不得包含服务端绝对路径、密钥或内部缓存文件名。

#### Scenario: 返回本地图片
- **WHEN** 检索命中本地图片
- **THEN** `results` 中的值可通过受控媒体接口访问，且不包含本机目录前缀

### Requirement: API 文档必须与规范契约一致
项目文档、OpenAPI 描述和前端调用 MUST 使用同一个规范入口、字段名、状态码和结果语义；任何兼容性破坏 MUST 在变更说明中标明。

#### Scenario: 文档与实现校验
- **WHEN** 对规范 API 执行契约测试
- **THEN** 文档示例中的请求可直接调用并符合响应 schema
