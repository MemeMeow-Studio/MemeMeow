# visual-similarity-search Specification

## Purpose
为 Agent 提供当前 scope 内基于已持久化图像向量的相似 Meme 检索，使其优先复用已完成研究的结构化语境，同时保证匹配过程不触发模型推理、不扩大数据范围，也不把视觉相似度误当作身份或出处证据。
## Requirements
### Requirement: 系统必须持久化版本明确的视觉向量
系统 MUST 为成功入库的图片异步生成视觉向量，并将向量与 `scope_id`、稳定 `meme_id`、图片 SHA-256、视觉模型标识、向量维度和预处理版本共同持久化。视觉向量 MUST 与文本语义向量分开管理；只有与当前图片内容指纹一致的向量才可用于查询或候选匹配。

#### Scenario: 视觉向量成功生成
- **WHEN** 当前 scope 中一张图片的视觉向量任务成功完成
- **THEN** 系统保存可回查模型和预处理身份的向量，并将其标记为对应当前图片 SHA-256 的有效产物

#### Scenario: 图片内容在生成期间变化
- **WHEN** 视觉向量任务完成时图片当前 SHA-256 已不同于任务输入
- **THEN** 系统拒绝把旧向量写入当前图片，并以稳定错误结束或重试该任务

#### Scenario: 处理 GIF 图片
- **WHEN** 系统为 GIF 图片生成第一版视觉向量
- **THEN** 系统仅使用该 GIF 的第一帧和固定预处理规则生成向量

### Requirement: 匹配必须只使用已有向量
视觉匹配 MUST 使用数据库中已经成功生成的查询向量和候选向量执行相似度查询。匹配接口和 Skill 脚本 MUST NOT 加载视觉模型、触发即时向量生成或调用外部供应商；查询向量不存在时 MUST 返回稳定的 `query_embedding_not_ready` 错误。

#### Scenario: 查询向量已就绪
- **WHEN** 运行中的 Agent 语境任务请求视觉匹配且其图片已有当前模型的有效向量
- **THEN** 系统直接查询已有视觉向量并返回结果，不调用任何推理模型或外部供应商

#### Scenario: 查询向量尚未就绪
- **WHEN** 运行中的 Agent 语境任务请求视觉匹配但其图片没有当前模型的有效向量
- **THEN** 系统返回 `query_embedding_not_ready`，且不隐式生成向量或等待轮询

#### Scenario: 向量空间不一致
- **WHEN** 查询向量与候选向量的模型、维度或预处理身份不一致
- **THEN** 系统不得比较这些向量，并返回稳定错误或排除不一致候选

### Requirement: 匹配必须受任务 scope 和候选资格约束
系统 MUST 在 Agent 启动前从可信任务上下文推导查询 `scope_id` 和查询 `meme_id`，并冻结候选图片清单。候选 MUST 属于同一 scope、图片记录和存储对象仍有效、视觉向量对应当前图片 SHA-256，并且当前图片内容已经由 research Agent 成功生成 `ready` 语境；`pending`、`partial`、`repair_required`、已删除和跨 scope 图片 MUST NOT 参与匹配。Agent 运行期间不得通过接口重新提交 scope 或候选数量。

#### Scenario: 返回同 scope 的 Agent-ready 候选
- **WHEN** 查询图向量有效且同 scope 中存在视觉相似、当前 Agent 语境已成功生成的图片
- **THEN** 系统只返回满足全部候选资格的图片，并默认排除查询图片自身

#### Scenario: 候选尚未完成 Agent 研究
- **WHEN** 视觉相似图片已有有效视觉向量但其语境仍为 `pending`、`partial` 或 `repair_required`
- **THEN** 系统不返回该图片

#### Scenario: 请求试图跨 scope 匹配
- **WHEN** 调用方引用不属于当前任务 scope 的图片或候选
- **THEN** 系统按资源不存在处理，并且不泄露其他 scope 的图片、向量或语境

### Requirement: 匹配结果必须便于 Agent 先读文本再选图
匹配响应 MUST 为每个候选返回稳定 `meme_id`、同一模型空间内的相似度分数、Agent 可访问的受控图片路径或媒体引用，以及已校验的结构化语境。结果 MUST 按相似度降序排列，分数相同时按稳定 `meme_id` 排序，且不得返回重复候选；相似度分数 MUST NOT 被表述为身份、出处或梗义置信度。

#### Scenario: 返回结构化候选
- **WHEN** 匹配请求找到多个合格候选
- **THEN** Agent 可以先读取每个候选的结构化语境，并只打开少量需要视觉核验的图片

#### Scenario: 相似度相同
- **WHEN** 多个候选具有相同相似度分数
- **THEN** 系统按稳定 `meme_id` 返回可重复的顺序

### Requirement: Agent 只能读取服务端冻结的候选清单
服务端 MUST 在 Agent 启动前生成并校验当前任务的候选图片清单。Skill 只能读取运行时注入的清单文件，不得调用本地视觉匹配 callback，不得提交 scope、候选数量或任意图片标识来扩大候选范围。

#### Scenario: Agent 读取候选清单
- **WHEN** Agent 需要参考本地相似图片
- **THEN** Skill 从当前任务清单读取候选及其语境、分数和受控图片引用
- **AND** 不创建新的本地匹配请求

#### Scenario: 候选清单缺失
- **WHEN** Agent 任务缺少服务端候选清单或清单校验失败
- **THEN** Skill 返回稳定错误或空候选诊断
- **AND** 不自行查询数据库或调用旧 callback
