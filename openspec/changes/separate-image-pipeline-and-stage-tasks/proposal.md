## Why

当前图片入口统一创建父处理 Job，能够可靠编排固定三阶段，却无法表达用户只想重试一个阶段的操作。把“从某阶段继续并执行下游”兼作独立重试会错误触发不需要的 Agent 或文本 embedding，也会让任务页无法区分一次完整处理和一个用户手动发起的单阶段任务。

## What Changes

- 新增图片处理提交模式：完整图片处理 Job 与独立阶段任务是两种显式、互不混淆的操作。
- 保留上传、图片库一键处理和完整重试的 Job 语义：由父 Job 按固定顺序协调三个阶段，并保留现有阶段有效性和失败停止规则。
- 新增用户可发起的独立 `visual_embedding_generation`、`meme_context_generation` 与 `text_embedding_generation` 任务；每次独立提交只执行所选阶段，不创建父 Job，也不调度上游或下游阶段。
- 禁止把任意图片阶段 Task 通过通用 Task 重试端点直接重试；完整重试必须针对 Job，独立重试必须通过受限的图片阶段提交入口。
- 在图片库和任务工作区分别展示完整 Job 的阶段层级与独立阶段任务，并提供“完整重试”和各阶段“仅重试”这两类明确操作。
- Agent 的独立阶段提交继续经过 operation policy、可信 grant、scope、图片 SHA 和 callback fencing；前端不得提交授权事实或内部任务关联。

## Capabilities

### New Capabilities

- `image-stage-submission`: 定义完整图片处理 Job 与独立图片阶段任务的提交、去重、授权、安全边界、状态和重试语义。

### Modified Capabilities

- `task-status`: 区分 Job 所属叶子任务和独立阶段任务的重试资格，防止通用 Task 重试绕过图片处理编排。
- `frontend-workbench`: 在图片库与处理任务工作区展示完整处理 Job 的阶段关系，并暴露完整重试和独立阶段重试两种操作。

## Impact

- 影响图片处理提交/重试 API、`ImageProcessingWorker` 与任务服务的任务归属和去重逻辑、operation policy/grant 集成，以及图片库和任务工作区。
- 复用既有 `image-processing`、application scope、任务 claim fencing、Agent callback 认证和反向图片策略约束，不改变图片内容、scope 或外部 callback 的可信来源。
- 需要补充 PostgreSQL 任务关联字段或等价持久化事实、API/Worker/前端测试及安全回归测试。
