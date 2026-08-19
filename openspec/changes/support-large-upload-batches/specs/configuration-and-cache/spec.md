## ADDED Requirements

### Requirement: 服务端必须公开上传能力边界

`/config` MUST 暴露非敏感的 `max_files_per_request`（固定默认 20）、仅供客户端调度器使用的 `max_concurrent_upload_requests`（固定默认提示 2），并以 `null` 或等价 disabled 值表示未配置的 `max_request_bytes`。配置响应不得暴露密钥、宿主路径或内部存储信息；服务端只对文件数和总字节预算执行上传边界，不把并发提示字段作为在途请求 admission。

#### Scenario: 默认上传配置

- **WHEN** 服务未配置总请求字节预算
- **THEN** `/config` 返回 `max_files_per_request: 20`、`max_concurrent_upload_requests: 2` 和 `max_request_bytes: null`
- **AND** 响应不包含 64 MiB 默认值或任何秘密
- **AND** `max_concurrent_upload_requests` 仅作为客户端调度提示

#### Scenario: 部署配置总预算

- **WHEN** 部署设置有效的 `max_request_bytes`
- **THEN** `/config` 返回该非敏感正整数
- **AND** 前端可以据此选择切片策略

#### Scenario: 非法配置

- **WHEN** 文件数、客户端并发提示或总字节预算配置不满足正整数和公开上界
- **THEN** 服务启动校验失败
- **AND** 不以宽松值运行
