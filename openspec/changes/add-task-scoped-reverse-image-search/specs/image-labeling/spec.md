## ADDED Requirements

### Requirement: 所有语境生成入口必须提供一致的反向图片策略选择
系统 MUST 在上传后自动生成、单图显式生成、图片库一键处理和终态 job 重试入口提供本次处理的 `forbid`/`auto` 选择。入口 MUST 将选择传给图片处理服务，由服务端规范化并冻结到每个图片处理 job revision，再由 `ImageProcessingWorker` 复制到实际创建的 `meme_context_generation` Task；客户端不得直接创建或覆盖叶子 Task payload。界面 MUST 使用可理解的用户文案说明 `auto` 允许将图片发送给第三方反向图片服务，且默认选择 `forbid`。

#### Scenario: 上传后自动生成
- **WHEN** 用户上传图片并选择一种反向图片策略
- **THEN** 每个上传成功后创建的图片处理 job 都冻结该策略，后续 Agent Task 使用相同策略

#### Scenario: 图片库一键处理
- **WHEN** 用户为一次图片库一键处理选择反向图片策略
- **THEN** 本次逐图创建的新 job revision 和必要的 Agent Task 使用相同策略，单项失败或被阻止不改变其他项的策略

#### Scenario: 终态 job 重试
- **WHEN** 用户为 `failed`、`blocked` 或 `unknown_execution` 的图片处理 job 选择策略并重试
- **THEN** 新 job revision 和必要的新 Agent Task 使用本次重试选择的策略，界面不把旧策略静默当作不可更改配置

#### Scenario: 用户没有主动允许
- **WHEN** 用户未更改任一生成入口的默认策略
- **THEN** 前端提交 `forbid`，不得因后端已配置供应商而自动提升权限

### Requirement: 自动策略不可用时必须在提交边界明确反馈
系统 MUST 向前端暴露不含密钥的反向图片服务可用状态。供应商配置缺失时，前端 MUST 禁用或明确标记 `auto` 不可用，后端 MUST 拒绝新建 `auto` 图片处理 job revision 并返回稳定错误；`forbid` job 仍可正常生成语境。

#### Scenario: 供应商未配置
- **WHEN** 用户打开生成入口且后端未配置反向图片供应商
- **THEN** 界面显示 `auto` 当前不可用，同时保留可选择的 `forbid`

#### Scenario: 客户端绕过界面提交自动策略
- **WHEN** 后端未配置供应商但收到 `reverse_image_policy=auto`
- **THEN** 后端拒绝创建图片处理 job revision 和叶子 Task，并返回 `reverse_image_unavailable`
