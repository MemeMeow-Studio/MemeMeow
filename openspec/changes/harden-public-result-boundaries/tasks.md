## 1. 公开边界工件和核心校验

- [x] 1.1 完成 `backend/public_dto.py` 的 Agent 字段白名单、敏感信息扫描、标识/时间/错误清理和按任务类型结果投影，并补齐类型守卫。
- [x] 1.2 在 OpenCode runner 和 executor 结果文件接收边界复用校验，确保敏感结果整体拒绝。

## 2. 任务与图片处理响应

- [x] 2.1 让 `TaskRecord`、磁盘历史和 PostgreSQL ORM 转换使用显式安全字段，默认不公开 payload，并收窄恢复历史。
- [x] 2.2 让图片处理快照、阶段、warning 和 error 使用公开 DTO，清理脏状态、时间和消息。
- [x] 2.3 收窄 HTTP 任务摘要从 payload 派生的策略、模型、meme 标识和文件名，防止动态字段泄漏。

## 3. 回归测试与验证

- [x] 3.1 增加恶意 Agent 输出、凭据变体、脏任务历史、脏图片阶段和恢复历史的单元/API 测试。
- [x] 3.2 运行相关 pytest、全量 Python 测试、compileall、diff 检查、OpenSpec 严格校验和 secret/path 静态扫描。
- [x] 3.3 完成一次安全代码 review，修复所有 P1/P2 风险并重新运行最终验证。
