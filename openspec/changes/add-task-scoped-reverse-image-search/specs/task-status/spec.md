## ADDED Requirements

### Requirement: 语境任务必须报告反向图片检索审计摘要
每个语境生成任务进入终态时，任务详情 MUST 返回反向图片审计摘要，至少包含持久化策略、是否请求过内部接口、是否使用过反向图片结果、缓存命中数、实际供应商调用数和最终结果。成功写回图片语境时，系统 MUST 将同一摘要写入图片 provenance；Agent 自述不得覆盖后端根据内部接口用量记录计算的摘要。

#### Scenario: 禁止策略任务未请求检索
- **WHEN** `forbid` 任务完成且未请求内部反向图片接口
- **THEN** 任务摘要显示 `policy=forbid`、`attempted=false`、`used=false`、缓存命中和供应商调用均为零

#### Scenario: 自动任务命中缓存
- **WHEN** `auto` 任务请求并成功使用缓存中的反向图片结果
- **THEN** 任务摘要显示 `attempted=true`、`used=true`、缓存命中数增加且实际供应商调用数不增加

#### Scenario: 自动任务实际调用供应商
- **WHEN** `auto` 任务的一个检索请求未命中缓存并开始供应商调用
- **THEN** 任务摘要的实际供应商调用数增加一次，并反映该请求的成功、空结果或失败状态

#### Scenario: Agent 在检索后失败
- **WHEN** Agent 已请求反向图片检索但语境任务最终失败
- **THEN** 失败任务仍保留根据后端用量记录生成的反向图片审计摘要

### Requirement: 活动语境任务去重必须保持策略一致
系统 MUST 继续阻止同一图片内容被多个活动语境任务并发处理。相同图片内容且策略相同的重复提交 MUST 复用现有活动任务；策略不同的重复提交 MUST 返回可诊断冲突，不得复用策略不一致的任务或并发覆盖同一图片语境。

#### Scenario: 相同策略重复提交
- **WHEN** 同一图片内容已有活动任务且新请求使用相同 `reverse_image_policy`
- **THEN** 系统返回或关联现有任务，不创建第二个活动任务

#### Scenario: 不同策略重复提交
- **WHEN** 同一图片内容已有活动任务但新请求使用不同 `reverse_image_policy`
- **THEN** 系统返回 `generation_policy_conflict`，不复用现有任务且不创建并发任务
