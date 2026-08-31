## MODIFIED Requirements

### Requirement: 语境研究必须把本地视觉候选当作参考证据

research Agent MUST 在读取后端提供的候选 manifest 后，将候选语境和图片仅作为研究参考；视觉相似度、候选排序或候选 context 单独不能证明图片身份、出处、模板、梗义或当前语用。候选 snapshot 的版本和 hash 由后端提供，Agent 不得改写。

#### Scenario: 候选可用

- **WHEN** 语境任务 workspace 中存在候选 manifest
- **THEN** Agent 可以先阅读候选 JSON，再按需打开少量候选图片，并保留自身不确定性和视觉核验

#### Scenario: 候选为空

- **WHEN** manifest 的 candidates 为空
- **THEN** Agent 正常继续独立研究，不把空列表解释为图片不存在或视觉模型失败
