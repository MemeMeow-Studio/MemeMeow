## ADDED Requirements

### Requirement: 上传请求必须有界且保持逐文件部分成功

`/images/upload` MUST 接受最多 20 个文件；超过上限的请求 MUST 在任何文件 durable 写入前以稳定错误拒绝。每个文件仍 MUST 独立校验、保存并返回结果，单文件默认 20 MiB 限制、扩展名和图片内容校验不得因总请求预算关闭而失效。部署可以配置正整数 `max_request_bytes`，启用时请求中所有文件字节总量不得超过该预算；未配置时总请求预算为 disabled，系统不得隐式使用 64 MiB 或其它总量上限。

#### Scenario: 21 个文件请求被拒绝

- **WHEN** 客户端向 `/images/upload` 提交 21 个文件
- **THEN** 服务端返回稳定的请求过大或文件数错误
- **AND** 该请求不产生任何 Meme、storage operation 或处理任务

#### Scenario: 20 个文件请求允许逐项处理

- **WHEN** 客户端提交恰好 20 个文件且每个文件满足现有校验
- **THEN** 服务端为每个文件返回独立结果
- **AND** 单个文件失败不会回滚其它成功文件

#### Scenario: 未配置总请求预算

- **WHEN** `max_request_bytes` 为 `None` 且请求文件总量超过 64 MiB 但每个文件不超过单文件限制
- **THEN** 服务端不因总量超过 64 MiB 而拒绝请求
- **AND** 每个文件仍执行单文件大小和图片内容校验

#### Scenario: 配置总请求预算

- **WHEN** `max_request_bytes` 已配置且请求文件总量超过该值
- **THEN** 服务端拒绝超出预算的文件或请求并返回稳定的请求字节错误
- **AND** 服务端不依赖客户端 `Content-Length` 头判断是否超限

### Requirement: Durable 上传重试必须幂等认领既有事实

当同一 scope/namespace 中存在规范化文件名、SHA-256 和大小均相同的 Meme，且数据库记录与存储文件事实一致时，重复上传 MUST 返回既有 `meme_id` 及当前可见处理状态，并视为幂等成功；不得重复创建 Meme、storage operation、处理 job 或叶子 task。同名但 SHA-256 或大小不同 MUST 稳定返回冲突。文件或数据库事实不一致时 MUST 返回 reconciliation 或稳定完整性错误，不得盲目认领。

#### Scenario: 响应丢失后重复提交

- **WHEN** 客户端再次提交同 scope、同规范化文件名、同 SHA-256 和同大小的图片
- **THEN** 服务端返回原 `meme_id` 和当前处理状态
- **AND** 数据库中的 Meme、storage operation 和处理任务数量不增加

#### Scenario: 同名不同内容

- **WHEN** 同 scope 中目标规范化文件名已存在但 SHA-256 或大小不同
- **THEN** 服务端返回稳定冲突错误
- **AND** 不覆盖原文件或创建新的同名 Meme

#### Scenario: 数据库与存储事实不一致

- **WHEN** 目标文件或数据库记录的大小、SHA-256 与另一侧不一致
- **THEN** 服务端返回可诊断的 reconciliation 错误
- **AND** 不把该上传认领为成功

#### Scenario: scope 隔离

- **WHEN** 另一 scope 中存在相同规范化文件名、SHA-256 和大小的图片
- **THEN** 当前 scope 的上传不复用另一 scope 的 `meme_id`
- **AND** 处理状态仅来自当前 scope
