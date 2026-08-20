## Why

Agent 输出、历史任务 JSON、图片处理阶段和恢复诊断都可能包含模型配置、宿主路径、凭据、scope 身份或执行绑定。当前不同 API 出口各自组装字段，导致新旧记录和异常分支存在绕过脱敏的路径；公网服务需要在接收边界和响应边界统一 fail-closed。

## What Changes

- 对 Agent 结果执行顶层字段白名单和递归敏感数据检查；命中 URL userinfo、敏感查询参数、内部地址、绝对路径、凭据或内部字段时整体拒绝。
- 为任务结果、任务摘要、恢复历史和图片处理快照建立显式公开 DTO，仅投影各任务类型允许的字段。
- 清理历史任务与图片处理记录中的非法状态、错误、时间、文件名和恢复标识，避免旧数据原样回显。
- 在 executor、OpenCode、PostgreSQL 任务转换及 HTTP 摘要出口重复执行边界校验，保证单一出口失误不会泄漏敏感内容。
- 增加恶意 Agent 输出、脏历史任务、脏图片阶段和恢复诊断的回归测试。

## Capabilities

### New Capabilities

- `public-result-boundaries`: 定义不可信 Agent 结果、任务结果和图片处理诊断的公开字段边界。

### Modified Capabilities

- `openspec/specs/agent-result-artifact/spec.md`: 增加 Agent 结果白名单、敏感信息整体拒绝和接收前验证要求。
- `openspec/specs/task-status/spec.md`: 增加任务结果、恢复历史和图片处理状态的显式安全 DTO 要求。
- `openspec/specs/image-ingestion/spec.md`: 增加图片处理阶段和自动命名 warning 的公开字段约束。

## Impact

- 影响 `backend/public_dto.py`、`backend/opencode.py`、`executor/server.py`、`backend/tasks.py`、`backend/agent_resume.py`、`backend/image_processing.py`、`backend/pg_services.py` 和 `api.py` 的数据边界。
- 任务列表和图片处理轮询响应不再暴露内部 payload、scope 或宿主路径；历史脏字段会被省略或收窄为稳定错误。
- 不新增外部依赖；通过单元测试、API 测试、全量 Python 测试和静态检查验证。
