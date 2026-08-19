## ADDED Requirements

### Requirement: 批量上传重试必须复用逐图片处理事实

上传客户端的逻辑批次 MUST NOT 改变现有逐图片任务状态机或创建跨请求持久 batch 实体。幂等上传返回的处理任务 MUST 属于当前 scope 和图片指纹，并继续通过现有任务接口展示可见状态；重复提交不得创建第二份活动任务。

#### Scenario: 幂等上传返回已有任务

- **WHEN** 同一 durable 图片上传被重复提交且其处理任务仍处于 queued 或 running
- **THEN** 响应返回同一可轮询处理任务标识
- **AND** 任务状态仍遵循 `queued -> running -> succeeded/failed`

#### Scenario: 终态失败不被伪装为成功

- **WHEN** 同一图片的既有处理任务已失败
- **THEN** 重复上传结果保留可诊断失败状态并允许现有显式重试入口恢复
- **AND** 上传重试不自动重跑已成功的处理阶段

#### Scenario: 不新增持久批次实体

- **WHEN** 用户一次选择的文件被多个 HTTP 请求上传
- **THEN** 服务端继续以单图片 Meme、storage operation 和 processing job 作为持久事实
- **AND** 页面刷新后未发送的本地文件不在服务端恢复
