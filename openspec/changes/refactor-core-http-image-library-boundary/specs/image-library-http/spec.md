## Purpose

为当前 scope 提供以稳定 `meme_id` 为身份的图片列表、metadata 详情和媒体读取接口，保持
文件指纹、状态投影和路径安全边界。

## ADDED Requirements

### Requirement: Image library routes remain compatible

系统 MUST 继续注册 `GET /images`、`GET /images/metadata` 和 `GET /media/{meme_id}`，保持
method、tag、query 参数、分页字段、响应 key 和旧 handler import。

#### Scenario: Canonical routes keep metadata

- **WHEN** 应用完成装配
- **THEN** 三个 route 各自只注册一个 canonical GET，继续保持与图片处理 route 的既有注册
  顺序，metadata 和 media 继续使用 `images` tag。

### Requirement: Image list is scope-bound and path selectors are rejected

列表 MUST 只从当前 scope repository 派生 Meme、metadata、embedding、visual 和 processing
事实；query 中出现目录、scope、user 或其它未知字段 MUST 返回 `400/invalid_request`。

#### Scenario: List projects current scope state

- **WHEN** 请求携带合法 search/page/page_size
- **THEN** 返回 `items`、`total`、`page`、`page_size`，每项包含 `meme_id`、文件名、媒体 URL、
  metadata/embedding/visual 状态，并在存在时附带最新 processing 摘要。

### Requirement: Metadata and media use stable meme identity

详情和媒体 MUST 只接受服务端派生的 `meme_id`；metadata service MUST 在返回或发送文件前
校验当前 scope 的 BlobStore 路径及数据库 SHA/size。

#### Scenario: Metadata requires meme id and projects stable errors

- **WHEN** 缺少 `meme_id` 或图片不存在/指纹不匹配
- **THEN** 分别返回 `400/meme_id_required` 或既有 `404/meme_not_found`/metadata 稳定错误，
  不暴露物理路径。

#### Scenario: Media returns the verified file

- **WHEN** 当前 scope 的 `meme_id` 对应图片通过 metadata service 指纹校验
- **THEN** 返回 `FileResponse`，媒体类型按受控文件名推断；未知 Meme 返回 `404/meme_not_found`。

### Requirement: Image library module dependencies remain one-way

公共图片库 HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；可变 scope/database/processing
依赖 MUST 通过 callback 注入。

#### Scenario: Dependency boundary is preserved

- **WHEN** 静态检查新模块和旧 import
- **THEN** 新模块无入口反向导入，旧 handler 名称仍可调用且 route 数量不增加。
