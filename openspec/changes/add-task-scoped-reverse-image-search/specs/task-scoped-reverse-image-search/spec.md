## Purpose

为每次图片语境任务提供显式、可持久化的反向图片检索策略，并通过供应商无关的内部接口统一缓存、调用、计数与审计，使 Agent 无需接触供应商密钥。

## ADDED Requirements

### Requirement: 反向图片检索策略必须由任务输入决定
系统 MUST 只接受 `forbid` 和 `auto` 两种 `reverse_image_policy`。客户端处理请求只提供受校验的业务选项；系统 MUST 将规范化策略冻结到图片处理 job revision，并由 `ImageProcessingWorker` 复制到其创建的 `meme_context_generation` Task 输入，客户端不得直接创建或覆盖叶子 Task payload。客户端未提供或历史 job/Task 缺少该字段时 MUST 使用 `forbid`。`auto` 表示允许 Agent 根据证据缺口按需调用，不表示每个任务都必须调用。

#### Scenario: 创建禁止检索的任务
- **WHEN** 客户端以 `reverse_image_policy=forbid` 请求图片处理
- **THEN** 系统将 `forbid` 冻结到 job revision 和必要的 Agent Task，且该 Task 的反向图片检索请求被拒绝

#### Scenario: 创建自动决定的任务
- **WHEN** 客户端以 `reverse_image_policy=auto` 请求图片处理
- **THEN** 系统将 `auto` 冻结到 job revision 和必要的 Agent Task，并允许 Agent 在需要时请求反向图片检索

#### Scenario: 请求未提供策略
- **WHEN** 新请求或待恢复的历史 job/Task 不包含 `reverse_image_policy`
- **THEN** 系统按 `forbid` 执行，不从进程环境或当前全局设置推断任务策略

#### Scenario: 请求提供未知策略
- **WHEN** 客户端提交 `forbid` 和 `auto` 之外的策略值
- **THEN** 系统返回可诊断的请求校验错误，且不创建任务

### Requirement: Agent 必须通过统一内部接口请求反向图片检索
系统 MUST 提供供应商无关的内部反向图片检索接口和保持相同输入输出语义的 CLI 客户端。接口 MUST 先验证 Agent callback 服务凭据和当前 Task 执行绑定，再根据受验证的 `task_id` 读取服务端任务记录中的策略；不得接受调用方自行声明或覆盖 scope、策略、claim、目标 Meme 或目标 SHA。系统 MUST 只接受存在、类型为 `meme_context_generation`、处于 `running`、持有非零当前 claim 和未过期租约的 Task。检索图片 MUST 等于该 Task 的目标 SHA，或由后端从该目标生成并绑定到当前执行的受控派生图；调用方不得借用一个任务检索任意图片。

#### Scenario: 自动任务请求检索
- **WHEN** 已认证 CLI 客户端为处于运行状态、当前 claim 有效且策略为 `auto` 的语境任务提交绑定目标的合法图片和检索参数
- **THEN** 内部接口执行缓存感知的反向图片检索并返回统一 JSON

#### Scenario: 禁止任务请求检索
- **WHEN** 已认证 CLI 客户端为策略为 `forbid` 的当前语境任务请求反向图片检索
- **THEN** 内部接口返回 `reverse_image_forbidden`，且不读取缓存、不联系供应商、不增加调用次数

#### Scenario: 无效任务请求检索
- **WHEN** 调用方使用不存在、非语境生成、非运行、claim 为零、旧 claim 或租约过期的 Task 执行绑定
- **THEN** 内部接口返回不泄露具体原因的稳定执行无效错误
- **AND** 不读取缓存、不记录 usage、不执行供应商调用

#### Scenario: 未认证调用内部接口
- **WHEN** 调用方缺少有效服务凭据，或 callback 验证能力未装配
- **THEN** 接口在读取 multipart 图片和查询 Task 前返回统一未认证错误
- **AND** 不回退到内网信任、用户身份或 local scope

#### Scenario: 使用任务检索任意图片
- **WHEN** 已认证调用方提交的图片既不匹配 Task 目标 SHA，也不是后端绑定到该目标和当前执行的受控派生图
- **THEN** 接口返回稳定执行无效错误
- **AND** 不读取缓存、不记录 usage、不联系供应商

### Requirement: 供应商密钥必须留在后端边界内
系统 MUST 只在后端反向图片服务中读取供应商密钥。Agent、CLI 客户端、任务输入、任务结果、缓存快照、用量记录和错误响应 MUST NOT 获得或泄露供应商密钥及临时上传凭据。

#### Scenario: 启动 Agent 任务
- **WHEN** Runner 为任意策略启动 Agent 进程
- **THEN** Agent 环境不包含 SerpApi 密钥、callback 根 secret 或 API 到 executor 的 Bearer token，只包含内部接口地址、任务标识和绑定当前执行的最小 callback 凭据

#### Scenario: 供应商返回私有标识
- **WHEN** 供应商响应包含密钥、临时图片标识或供应商归档地址
- **THEN** 后端在缓存、用量记录和接口响应之前移除这些字段

### Requirement: 缓存检查和实际调用必须统一互斥
系统 MUST 使用图片内容和影响结果的规范化检索参数生成稳定缓存键，并 MUST 在同一缓存键的互斥范围内完成二次缓存检查和供应商调用。有效缓存命中 MUST 直接返回且实际供应商调用数为零；未命中、过期或明确刷新时才可发起供应商调用。

#### Scenario: 有效缓存命中
- **WHEN** 相同图片内容和检索参数已有有效缓存快照
- **THEN** 系统返回缓存结果、标记 `cache.status=hit` 和 `provider.called=false`，且不增加实际供应商调用次数

#### Scenario: 缓存未命中
- **WHEN** 相同缓存键不存在有效快照
- **THEN** 系统在持有该键互斥锁时只发起一次逻辑供应商检索，并按结果写入脱敏快照

#### Scenario: 并发请求相同缓存键
- **WHEN** 多个允许检索的任务同时请求相同图片内容和检索参数
- **THEN** 至多一个请求发起实际供应商调用，其余请求在锁内复查后复用新缓存

### Requirement: 实际供应商调用必须按 scope 幂等计数
系统 MUST 为每次内部接口请求生成稳定 `request_id` 并在数据库记录所属 `scope_id`、`task_id`、图片、缓存状态、是否开始供应商调用、供应商结果和时间。只有缓存未命中且已经开始联系供应商的逻辑检索 MUST 为该 scope 增加一次实际调用；底层图片上传和 Lens 查询不得被分别计数。缓存命中不得增加调用次数，重复写入同一 `request_id` 不得重复计数。

#### Scenario: 未命中并开始供应商调用
- **WHEN** 后端完成缓存未命中判断并开始联系供应商
- **THEN** 系统为当前 scope 记录恰好一次实际调用，即使结果为空或后续失败

#### Scenario: 底层包含多个 HTTP 步骤
- **WHEN** 一次逻辑反向图片检索先上传本地图片再提交 Lens 查询
- **THEN** 数据库中的实际调用次数只增加一次

#### Scenario: 同一请求被重复提交
- **WHEN** 相同 `request_id` 因重试或恢复被再次写入
- **THEN** 系统复用已有用量记录，不再次增加实际调用次数

#### Scenario: Agent 请求多个不同检索
- **WHEN** Agent 分别对目标整图和后端从该目标生成的一个受控裁剪产生两个缓存未命中的逻辑检索
- **THEN** 系统分别记录两次实际供应商调用

### Requirement: 内部接口必须返回稳定且供应商无关的结果
内部接口 MUST 返回请求标识、缓存状态、是否调用供应商、规范化结果和稳定结果状态；供应商不可用、返回非法数据或超时时 MUST 返回稳定错误和是否可重试信息，不得向 Agent 暴露供应商专有错误正文。

#### Scenario: 返回成功结果
- **WHEN** 缓存或供应商返回可用的反向图片候选
- **THEN** 接口返回 `request_id`、`cache`、`provider` 和脱敏 `result`，CLI 客户端原样输出该统一 JSON

#### Scenario: 供应商调用失败
- **WHEN** 供应商明确返回失败、空结果或完整但非法的响应
- **THEN** 接口返回稳定的反向图片错误与可重试标记，并保留已开始调用的用量记录

#### Scenario: 供应商调用结果无法确认
- **WHEN** usage 已记录 provider 调用开始，但网络中断或进程退出后无法验证同一 request id 的既有结果
- **THEN** 系统保留 usage 与计量事实，不重放 provider，并返回稳定 `reverse_image_unknown_execution` 和可降级标记
- **AND** `auto` Agent 继续离线分析；该反向图片子调用状态不得被冒充为整个 Agent Task 或图片处理阶段的 `unknown_execution`
