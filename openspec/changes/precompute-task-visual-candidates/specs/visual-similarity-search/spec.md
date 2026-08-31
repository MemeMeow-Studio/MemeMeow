## MODIFIED Requirements

### Requirement: 视觉匹配必须在 Agent 启动前生成并冻结 snapshot

系统 MUST 在 `meme_context_generation` 进入外部执行窗口前，使用当前 scope、当前图片 SHA 和冻结视觉模型身份执行固定、有界的已有向量匹配，并将查询身份、候选排序、候选 SHA、候选语境、匹配时间和 canonical snapshot hash 保存为 `visual_match_snapshot`。Agent MUST NOT 提交 top-k、查询任意 Meme 或调用视觉匹配 callback。

#### Scenario: 匹配成功并启动 Agent

- **WHEN** claim 后查询向量和所有选中的候选向量均满足当前模型、维度、预处理版本和图片 SHA 约束
- **THEN** 后端先持久化 protocol v2 snapshot，再提交 Agent grant、标记外部执行开始并启动 OpenCode
- **AND** Agent 获得的候选语境和排序来自该 snapshot

#### Scenario: 没有合格候选

- **WHEN** 查询向量有效但当前 scope 没有满足 Agent-ready 和存储身份约束的候选
- **THEN** 系统保存空 candidates 的成功 snapshot，并正常启动 Agent

#### Scenario: 预计算失败阻止外部执行

- **WHEN** 查询向量未就绪、图片 SHA 已变化、模型身份不一致或候选无法安全物化
- **THEN** 任务以稳定视觉错误失败或按该错误的显式重试策略重新排队
- **AND** 不提交 Agent grant、不标记 `external_started`、不启动 OpenCode

### Requirement: snapshot 必须对后续数据变化保持稳定

系统 MUST 在 snapshot 中深拷贝候选 `context`，并以 protocol version 和 snapshot SHA 校验完整内容。后续 Meme context、文件名或候选排序变化 MUST NOT 改写已被当前 attempt 引用的 snapshot。resume MUST 复用同一 snapshot；只有新 Task/revision 才能重新匹配。

#### Scenario: 后续语境修改

- **WHEN** 候选 Meme 在 Agent 运行期间被人工修改
- **THEN** 当前任务继续读取原 snapshot context，不能出现半新半旧候选

#### Scenario: resume

- **WHEN** Agent 外部执行失败后由新 claim 恢复同一 Task
- **THEN** 系统校验并复用原 snapshot hash，不重新查询向量或改变候选顺序

### Requirement: 候选结果必须保持 scope 和模型隔离

系统 MUST 继续只使用同 scope、当前图片 SHA、当前模型身份和 Agent-ready 的候选；空列表是唯一的“没有候选”结果。跨 scope、过期 storage operation、SHA/size 不一致和 context 未 ready 的候选 MUST 被排除或以稳定物化错误终止，不能泄露其标识或语境。

#### Scenario: 跨 scope 候选

- **WHEN** snapshot 或物化请求包含不属于任务 scope 的候选标识
- **THEN** 系统按资源无效处理并拒绝启动 Agent

### Requirement: 相似度只表示排序参考

候选 `score` MUST 只表示同一视觉向量空间内的相似度排序值，不能被任务结果或 Skill 描述为身份、出处或梗义置信度。结果 MUST 按 score 降序、Meme UUID 升序且不重复。

#### Scenario: Agent 使用候选分数

- **WHEN** Agent 读取 snapshot 中的候选分数和排序
- **THEN** 它只能将分数作为选择少量图片进行视觉核验的参考，不能把分数写成身份、出处或梗义结论
