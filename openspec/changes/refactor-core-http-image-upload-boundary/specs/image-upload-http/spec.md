## Purpose

为当前 scope 提供受控的图片上传 HTTP 边界，在 multipart 资源预算、图片安全预检、durable
持久化、operation 计量和后续处理任务之间保持可观察且可回归的公开契约。

## ADDED Requirements

### Requirement: 上传路由与请求字段保持兼容

系统 MUST 继续注册唯一的 `POST /images/upload` 路由并返回既有成功状态、`images` 标签和
`batch_id`/`results` 响应结构。请求 MUST 接受 `files`、`reverse_image_policy` 和 `auto_name`
字段，未知 multipart 字段 MUST 在任何 durable 写入前以稳定 `400/invalid_request` 拒绝。

#### Scenario: canonical route and fields

- **WHEN** 客户端向 `/images/upload` 提交一个或多个图片文件及受支持的处理选项
- **THEN** 系统返回 `200`，每个文件在 `results` 中拥有独立结果，并保持既有媒体 URL、文件名和
  处理任务兼容字段
- **AND** 应用路由表中该 canonical path 只出现一次，旧 `api.upload_images` handler 名称仍可导入

#### Scenario: unknown legacy field

- **WHEN** multipart 请求包含 `directory` 或其它未声明字段
- **THEN** 系统在创建 Meme、写入文件或 acquire upload operation 前返回 `400/invalid_request`

#### Scenario: file count limit

- **WHEN** 请求包含超过 20 个文件
- **THEN** 系统在任何文件 durable 写入前返回 `413/too_many_files`，且不产生部分成功记录

### Requirement: multipart 和图片资源边界必须有界

系统 MUST 在 multipart 解析阶段按实际文件字节执行可选的总请求预算，并在业务处理阶段对每个
文件执行既有单文件大小、扩展名和图片内容预检。缺失或不可信的 `Content-Length` 不得绕过
这些限制；批量请求中的单项校验失败 MUST 只影响该项。

#### Scenario: exact and exceeded request budget

- **WHEN** 所有文件字节总量恰好等于配置预算，或在 parser 返回表单前超过预算
- **THEN** 前者允许继续逐文件处理，后者返回 `413/request_too_large`，且超预算请求不进入
  durable 写入

#### Scenario: single-file validation

- **WHEN** 某文件扩展名不受支持、超过单文件上限、文件名非法或图片内容无法通过安全预检
- **THEN** 该文件结果返回稳定错误 code，服务继续报告其它文件的成功或失败，不读取下一文件
  之前的全部内容到一个无界请求缓存

### Requirement: 上传目标和 operation 事实必须绑定当前 scope

系统 MUST 通过当前可信 scope 的 metadata/blob service 派生文件目标，不得接受客户端 scope、
user 或目录选择器覆盖目标。通过图片名、大小和 SHA-256 验证三方 durable 事实一致的重复上传
MUST 返回幂等成功且不得重复 acquire；事实不一致 MUST 返回
`upload_reconciliation_required`。新 durable 上传 MUST 在写入前 acquire `image.upload`，写入
成功后 commit；只有明确没有 durable 副作用的错误才可 release，policy 不可用或拒绝时 MUST
fail-closed。

#### Scenario: scope-bound idempotence

- **WHEN** 当前 scope 重试同名、同大小且同 SHA-256 的已持久化图片
- **THEN** 系统返回原 Meme ID、`idempotent: true` 及当前处理状态，不创建第二条 Meme 或消耗
  新的 upload grant

#### Scenario: durable fact mismatch

- **WHEN** 数据库记录、目标文件大小或 SHA-256 任一事实不一致
- **THEN** 系统返回逐项 `upload_reconciliation_required`，不把目标当作可复用图片

#### Scenario: policy rejection and write failure

- **WHEN** `image.upload` acquire 被拒绝或策略不可用，或 metadata 明确报告 durable 写入前的
  稳定错误
- **THEN** 系统不写入文件或 Meme；拒绝映射为宿主既有 policy 错误，明确可补偿的失败才尝试
  release，未知 I/O 副作用不得被伪装为可退款失败

### Requirement: durable 成功后的任务和检索语义保持兼容

图片和 Meme durable 提交成功后，系统 MUST 返回既有 `meme_id`、`saved_filename`、`media_url`、
metadata 状态和处理任务兼容字段，并尝试提交当前 scope 的统一处理 Job 或旧视觉任务。处理
任务提交失败 MUST 作为可重试诊断返回而不得撤销已成功上传；检索失效 MUST 发生在 durable
成功之后。多文件请求 MUST 保留逐项部分成功和既有批次调度语义。

#### Scenario: processing warning after upload

- **WHEN** 图片已完成 durable 写入但处理 worker 不可用或提交失败
- **THEN** 结果仍报告上传成功，并以稳定 `processing_job_error` 或视觉任务错误字段说明可重试
  诊断，不删除图片或 Meme

#### Scenario: multi-file scheduling

- **WHEN** 一个请求成功提交多个新图片且使用兼容旧任务服务
- **THEN** 系统返回共享 `batch_id`，逐项任务先创建后按既有封存/调度顺序启动；统一处理 worker
  使用时不创建旧的 cache generation
