## MODIFIED Requirements

### Requirement: 重试必须限制在失败阶段
用户显式重试图片处理时，系统 MUST 区分完整 Job 重试和独立阶段重试。对终态 Job 的完整重试 MUST 创建新的 Job revision；只有同一新 revision 仍活动时的重复请求可以复用该活动 revision，并且旧终态 Job 保持不可变历史。完整 Job 只在其内部按固定顺序协调所需阶段，仍有效的前置产物可以复用。独立阶段重试 MUST 通过受限图片阶段提交入口创建或复用无 Job 关联的独立 Task，并只执行所选失败或过期阶段；终态独立 Task 的重试 MUST 创建新的逻辑 Task。所有图片阶段 Task MUST NOT 通过通用 `POST /tasks/{task_id}/retry` 直接重试；通用端点遇到此类 Task MUST 返回稳定的拒绝结果，不得复制其 payload 创建脱离编排约束的 Task。图片内容指纹变化属于新处理版本，不适用旧版本的阶段复用。

#### Scenario: 独立重试 Agent 失败
- **WHEN** 用户选择仅重试失败的 Agent 阶段
- **THEN** 系统创建或复用独立 Agent Task，且不得创建视觉或文本 embedding Task

#### Scenario: 重试 Agent 失败
- **WHEN** 用户通过受限图片阶段入口重试失败的 Agent 阶段
- **THEN** 系统创建新的独立 Agent Task，不重跑有效视觉向量，也不创建文本 embedding Task

#### Scenario: 完整 Job 重试 Agent 失败
- **WHEN** 图片处理 Job 的 Agent 阶段失败，用户选择完整重试
- **THEN** 系统保留旧 Job 历史并创建新的 Job revision，或复用并发请求已创建的活动新 revision
- **AND** 新 Job 复用仍有效的视觉向量，按 Job 顺序重新协调 Agent 及其后续必要阶段

#### Scenario: 重建视觉模型向量
- **WHEN** 用户或部署方为已有图片提交独立视觉模型向量任务
- **THEN** 系统不自动创建或重跑 Agent 语境和文本 embedding Task

#### Scenario: 拒绝重试图片阶段 Task
- **WHEN** 客户端调用通用 Task 重试端点重试任一视觉、Agent 或文本 embedding 图片阶段 Task
- **THEN** 系统拒绝该请求并返回稳定错误标识
- **AND** 系统不得创建新的叶子 Task、Job revision 或 Agent grant

#### Scenario: 图片内容发生变化
- **WHEN** 同一 Meme 的当前图片 SHA-256 不再等于旧任务和旧产物指纹
- **THEN** 系统为新图片版本创建新的必要处理任务，并拒绝复用旧内容指纹的视觉向量
