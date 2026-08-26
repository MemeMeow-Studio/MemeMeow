## 设计

`metering_units` 是公共核心的透明整数事实。核心只验证它是非负整数并将其写入 fingerprint 与 grant；宿主决定数值含义。数据库列允许历史 `NULL`，旧 fingerprint 仅在请求成本为 `0` 时按 legacy 规则读取，避免重写或伪造历史事实。新 acquire 始终写入明确的 `0` 或正整数；终态转换继续复用同一不透明 grant，不重新计算成本。
