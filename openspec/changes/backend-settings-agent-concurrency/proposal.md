## Why

OpenCode Agent 的并发数量将成为并行提取的核心运行参数，但当前配置由不可变的进程级 dataclass 读取，前端只有脱敏的 `/config` 状态，没有独立的后端配置工作面。需要用 Pydantic Settings 统一校验 `.env`/环境变量，并让操作者在明确的后端设置页查看配置边界、调整安全的并发数量并知道何时需要重启。

## What Changes

- 使用 Pydantic Settings 替换手工环境变量解析，保留现有变量名、进程环境优先级、默认值和 `.env` 兼容行为。
- 新增 `MEMEMEOW_OPENCODE_CONCURRENCY`，默认值为 `1`，限制在安全范围内，作为 OpenCode Agent lane 的并发上限。
- 新增独立的“后端设置”页面，与前端搜索、显示和交互偏好分离。
- 后端设置页按三类展示配置：只读后端状态、安全可调整参数、仅允许部署环境修改的高风险参数。
- 仅将 Agent 并发数量列为页面可调整项；修改后持久化到 `.env` 的非敏感字段，并明确标记“重启后生效”。环境变量覆盖存在时，页面必须显示其优先级而不是伪装保存成功。
- 继续对 API key、可执行文件路径、runtime/data 根目录、任意 provider URL 和接口白名单脱敏或只读，不向浏览器回传密钥。
- 新增后端设置查询/更新接口、配置版本/重启提示和前端页面状态；未配置设置管理凭据时，更新接口保持禁用。

## Capabilities

### New Capabilities

- `backend-settings`: 为操作者提供独立的后端配置状态、并发数量调整和部署参数边界。

### Modified Capabilities

- `task-status`: 任务记录和运行状态需要反映有效的 Agent 并发配置及其重启生效语义。

## Impact

- 影响 `backend/config.py`、`api.py`、任务/Agent 调度配置、`.env.example`、API 文档及 Vue 前端导航和设置页面。
- 新增 `pydantic-settings` 运行依赖，并保留现有 `python-dotenv` 脚本读取变量的兼容路径。
- 配置写入只允许更新非敏感并发字段，采用受保护的原子 `.env` 更新；当前进程不热更新，重启后新配置才生效。
- 需要新增配置校验、环境优先级、敏感字段隐藏、权限拒绝、并发范围、重启提示和桌面/移动端设置页测试。
