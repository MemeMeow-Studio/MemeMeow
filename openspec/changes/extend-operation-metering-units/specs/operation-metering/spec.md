## ADDED Requirements

### Requirement: 公共 operation 必须支持套餐无知的独立计量事实
`OperationRequest.units` MUST 继续表示正整数逻辑调用数。可信宿主 MAY 写入非负整数 `metering_units`，核心 MUST 验证其类型和值，并将其纳入请求 fingerprint、operation grant 持久化及幂等生命周期的完整请求事实。核心 MUST NOT 解释该数值的业务含义。

#### Scenario: 零成本与正成本请求
- **WHEN** 宿主分别创建 `metering_units=0`、`1000` 或 `2000` 的请求
- **THEN** 请求可通过公共校验且 `units` 仍为 `1`
- **AND** 不同计量成本的请求 fingerprint 不相同

### Requirement: 历史 grant 必须安全兼容
历史没有 `metering_units` 的 grant MUST 能沿其原有请求事实完成只读读取和 commit/release/unknown 收束；历史行不得被当作带有未知正成本的新请求。新的 acquire MUST 明确持久化 `metering_units`。
