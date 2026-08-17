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
系统 MUST 继续阻止同一 scope、Meme 和目标图片 SHA 被多个活动图片处理 job 或语境 Task 并发处理。只有目标 SHA、Agent/处理配置指纹和 `reverse_image_policy` 全部相同的重复提交才能复用现有活动 job/Task；策略或配置不同的重复提交 MUST 返回 `generation_policy_conflict`，不得复用不一致的执行或并发覆盖同一图片语境。

#### Scenario: 相同策略重复提交
- **WHEN** 同一 scope、Meme 和目标 SHA 已有活动 job/Task，且新请求使用相同 Agent/处理配置指纹和 `reverse_image_policy`
- **THEN** 系统返回或关联现有 job/Task，不创建第二个活动执行

#### Scenario: 不同策略重复提交
- **WHEN** 同一 scope、Meme 和目标 SHA 已有活动 job/Task，但新请求使用不同 Agent/处理配置指纹或 `reverse_image_policy`
- **THEN** 系统返回 `generation_policy_conflict`，不复用现有执行且不创建并发 job/Task
