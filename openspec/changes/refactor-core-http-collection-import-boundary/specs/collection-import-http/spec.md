## Purpose

为公共核心提供独立且 scope-safe 的合集 ZIP 导入 HTTP 边界，在保持包内容身份、资源上限、权限计量、副作用顺序和逐图片结果兼容的前提下，让入口层只负责装配与路由声明。

## ADDED Requirements

### Requirement: 合集导入路由和兼容入口保持稳定

系统 MUST 继续注册唯一的 `POST /collections/import` canonical route，保持 `collections` tag、成功 status、multipart `file` 字段、旧 `api.import_collection` handler import 和现有响应 key；新 HTTP 模块 MUST 不注册导入或导出 route，也不得重复注册合集 CRUD。

#### Scenario: 导入和导出路由只注册一次

- **WHEN** 公共应用完成装配并读取 route table
- **THEN** `/collections/import` 与 `/collections/{collection_id}/export` 各只出现一次
- **AND** 合集 CRUD route 仍各只出现一次，旧 `api.import_collection` 可调用且新模块不存在 `api` 或 `server_api` 反向 import

### Requirement: 上传和 ZIP 资源边界必须 fail-closed

合集导入 MUST 在任何数据库或文件副作用前拒绝非 multipart、缺失/多余 file 字段、非 ZIP 文件、超出压缩请求/单文件/成员数/解压总量/manifest/压缩比/图片帧或像素上限的输入；错误 MUST 使用既有稳定 status 和 error code，且不得暴露内部路径或异常文本。

#### Scenario: 非法 multipart 与压缩包被拒绝

- **WHEN** 请求包含未知表单字段、多个文件、非 `.zip` 文件、非法 ZIP、缺失 manifest 或超出任一压缩资源预算
- **THEN** 系统在创建合集、写入图片或 acquire operation 前返回既有 `400/413` 错误 code
- **AND** response 不包含服务器文件系统路径

#### Scenario: manifest 成员身份和路径不匹配

- **WHEN** manifest 的 `source_meme_id`、扩展名、`images/<source_meme_id><extension>` 路径、文件大小或 SHA-256 与 ZIP 实际成员不一致
- **THEN** 系统拒绝整个包并返回既有 manifest/path/size/SHA/format 错误 code
- **AND** 不创建合集、不写入 Meme/图片、不获取 operation grant

### Requirement: 导入必须绑定可信 scope 并隔离逐项失败

系统 MUST 只通过当前请求冻结的 scope environment 和 metadata BlobStore 查找或写入合集、Meme 与文件；客户端不得通过 query、表单或 manifest 覆盖 scope。跨 scope 同名/资源事实 MUST 不可见；单个成员失败 MUST 只影响该成员结果，不泄露其它 scope 数据。

#### Scenario: scope 选择器不能改变导入目标

- **WHEN** 导入请求携带 `scope_id`、`user_id` 或其它未知 query/表单字段
- **THEN** 系统返回 `400/invalid_request`，且不会打开 scope environment 或写入任何资源

#### Scenario: 当前 scope 名称冲突和逐成员结果

- **WHEN** manifest 合集名称已存在，或某个成员发生 filename conflict、成员存储故障、关系写入故障或任务提交故障
- **THEN** 合集名称冲突返回稳定 `409/collection_exists`；逐成员故障保留已成功成员并返回 `partial`、`results`、`meme_id_map` 及既有成员错误 code
- **AND** 失败成员不会被错误地报告为成功或映射到另一个 scope 的 Meme

### Requirement: 成员复用、operation policy 和副作用顺序保持兼容

同名且 identity 校验通过的图片 MUST 复用现有 Meme 并只建立当前 scope 合集关系；同名不同 SHA MUST 按既有 SHA 后缀规则选择安全文件名。新增图片 MUST 在 durable 写入前 acquire `IMAGE_UPLOAD`，写入成功后 commit；明确未发生 durable 写入的可恢复错误才 release，commit 不确定时不得虚假撤销成功事实。

#### Scenario: 同名复用与冲突重命名

- **WHEN** 包成员与当前 scope 已有文件同名同 SHA，或同名但 SHA 不同
- **THEN** 前者返回 `status: reused` 且不重复写入图片或 acquire upload operation，后者使用既有 SHA 后缀安全文件名并导入为新 Meme

#### Scenario: operation policy 拒绝和成功收束

- **WHEN** 新成员 operation acquire 被拒绝，或 durable 写入前发生可确认的 staging/文件名错误，或写入后 commit 返回错误
- **THEN** acquire 拒绝只生成该成员稳定 policy 错误且不写入图片；可确认未写入时尝试 release；写入后的 commit 故障不删除文件/Meme，也不将该成员伪装成未发生副作用

### Requirement: 成功投影和异步处理顺序保持稳定

新 Meme 与合集关系 durable 成功后，系统 MUST 保持现有 visual/processing task 投递顺序、父 Job/兼容字段、配置和 SHA 输入；处理服务不可用或投递失败 MUST 作为逐项稳定告警返回，而不回滚已写入图片。至少一个新 Meme durable 成功后才失效当前 scope 检索缓存。

#### Scenario: 新导入先进入视觉或统一处理任务

- **WHEN** 新图片成功写入并建立合集关系且任务服务可用
- **THEN** 响应保留 `visual_task_id`/`metadata_job_id` 或统一处理 Job 兼容字段，并使用 manifest SHA 绑定任务输入
- **AND** 不为同一导入伪造额外任务身份

#### Scenario: 任务投递失败不隐藏 durable 成功

- **WHEN** 图片和 Meme 已成功提交但视觉或处理任务投递失败
- **THEN** 对应结果保持 `ok: true`，返回稳定任务错误字段，整体 status 为 `partial`，并在有新 Meme 时执行一次检索失效
