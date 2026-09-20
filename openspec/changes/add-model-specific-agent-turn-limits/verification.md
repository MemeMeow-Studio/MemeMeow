# 主 session 金额控制验证

## 实现范围

Executor 在进程检查循环中读取传给 OpenCode 的 `OPENCODE_DB`，使用冻结主 session 的 `session.cost` 判断终止。插件通过 OpenCode SDK 读取同一主 session 金额。任务数据库和工作目录绑定检查、启动等待、金额单调检查及进程回收流程共同完成执行控制。

本次调整面向新建任务，不迁移已有任务的金额记录。模型请求继续使用现有配置和凭据。

## 验证结果

- `uv run pytest tests/test_analysis_usage.py tests/test_analysis_policy.py -q --basetemp=.pytest_cache/session-cost-final`：41 项通过。
- 使用实际 SQLite 连接验证主 session 选择、子 session 和目录拒绝、金额边界、金额减少、无效金额、数据库缺失、schema 不兼容、启动绑定期限、WAL 写事务期间读取及排他锁错误。
- Python 编译、插件 JavaScript 语法检查、`uv lock --check` 和 OpenSpec 严格校验通过。
- 开发 Agent 通过 `./scripts/agent-runtime.sh start` 完成构建与启动，镜像固定 OpenCode `1.18.18`，共享依赖沿用镜像安装。

## 真实任务环境阻塞

通过开发容器现有 Executor HTTP 接口提交带测试标识的图片任务。attempt 为 `session-cost-termination-14e20f3ee0424667b8bae6f14ecf78a0`，最终 `failed`，`process_reaped=true`，未产生主 session，观测金额为 0。

容器内 bubblewrap 返回 `No permissions to create new namespace`，OpenCode 未能启动。Executor 对外记录 `agent_analysis_usage_unavailable:session_binding_deadline_exceeded`。真实金额终止和提醒尚未通过验收；服务构建成功和单元测试通过均不能替代这些验收。

## 独立审查与处理

default 子代理审查了当前改动、任务数据库权限、调用路径和测试范围。

- 数据库信任边界：Agent 可以写入自身任务数据库。金额单调检查可以拒绝已观测到的金额减少，无法识别所有合法形式的数据库改写。本功能按照既定设计提供正常执行中的分析程度控制，保留这一限制；本次不增加数据库写入代理或操作系统身份隔离改造。
- 验证覆盖：已补充 WAL 并发写入及排他锁测试。完整 Executor 真实执行受上述 namespace 权限阻塞，保留为发布前待完成项目。

公共提交待用户审核；Server 同步和真实验收在授权后继续。
