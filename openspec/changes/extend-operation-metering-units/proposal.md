## Why

公共 operation 核心目前只能传递逻辑调用数 `units`，无法让可信宿主携带零成本或正成本的独立计量事实。将套餐、模型和支付概念放进公共核心会扩大边界，因此只增加套餐无知的整数扩展点。

## What Changes

- 为 `OperationRequest` 增加可信宿主写入的非负整数 `metering_units`。
- 将该字段纳入请求校验、幂等 fingerprint、operation grant 持久化和 acquire/commit/release/recovery 的请求事实校验。
- 为历史没有该字段的 grant 保留只读兼容收束路径；新请求默认以 `0` 写入。

## Non-Goals

- 不解析模型、套餐、账户、周期、价格或支付。
- 不改变 `OperationRequest.units` 的正整数逻辑调用语义。
