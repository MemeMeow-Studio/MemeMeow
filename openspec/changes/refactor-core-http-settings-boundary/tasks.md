## 1. 基线与边界确认

- [x] 1.1 在开源仓库记录目标文件和测试入口的干净/脏状态，确认没有与 `api.py`、`backend/config.py` 或 Settings 测试重叠的未提交修改，并确认实现阶段不触碰 MemeMeowServer。
- [x] 1.2 在实现前保存现有 Settings 路由表、OpenAPI 可见性和成功/错误响应 key 的基线快照，覆盖六个 method/path 组合及 handler 名称。
- [x] 1.3 将 `specs/backend-settings/spec.md` 的 URL、method、status、错误 code、输入别名、Header/Bearer 和环境覆盖 fail-closed 条件整理为可执行验收清单。

## 2. Settings HTTP 模块实现

- [x] 2.1 新建 `backend/settings_http.py`，补充中文文件/函数 docstring，迁移严格的并发 JSON 请求模型并保留四个输入别名、正整数约束和未知字段拒绝。
- [x] 2.2 迁移 Settings 状态投影，继续复用 `Settings.backend_status()`、当前 scope search cache 和 runtime/visual 布尔探针，保持所有脱敏字段、重复兼容 key、`saved`、`pending` 和 `restart_required` 语义。
- [x] 2.3 实现兼容 Header/Bearer token 提取与恒定时间校验，保留 Header 优先级、Bearer 大小写兼容、`settings_forbidden` 403 以及所有失败不产生副作用的 fail-closed 行为。
- [x] 2.4 迁移设置更新编排，继续在 dotenv 写入前拒绝 `MEMEMEOW_OPENCODE_CONCURRENCY` 环境覆盖，并复用 `update_dotenv_concurrency()` 的背压校验、原子写入和 `400/409` 稳定错误映射。
- [x] 2.5 以 `APIRouter` 注册 canonical/legacy 六个 method/path 组合，保持公开 schema 标志、tags、handler 名称、路由顺序和未注册 method 的 405 行为。

## 3. 应用装配与兼容导出

- [x] 3.1 在 `api.py` 原 Settings 路由模板位置接入并展开新 router，使模块级 `app` 和 `create_app()` 生成的宿主应用都得到同一组路由，不改变其它路由、middleware、lifespan 或 scope 装配。
- [x] 3.2 从 `api.py` 删除重复 Settings HTTP 实现，保留 `ConcurrencyUpdateRequest`、三个 route handler 和既有 Settings 私有 helper 的必要兼容 re-export，继续保留 `/config` 在 `api.py`。
- [x] 3.3 收窄导入依赖并执行静态检查，证明 `backend/settings_http.py` 不反向 import `api.py`，且不引入第三方依赖、schema/migration、frontend、search 或 server adapter 改动。

## 4. 高信号契约与回归测试

- [x] 4.1 增加 Settings HTTP 契约测试和路由表快照，逐项断言 canonical/legacy path、method、handler、tags、OpenAPI 隐藏标志、200/405 状态及成功响应 key 集合。
- [x] 4.2 增加输入别名与严格校验矩阵，覆盖四个别名、未知字段、缺失字段、布尔值、字符串数字、零值和负值，并断言 `400/invalid_request` 且没有 dotenv 写入。
- [x] 4.3 增加授权矩阵，覆盖两个 token Header、Bearer 大小写、Header 优先级、缺失/错误/未配置凭据和格式错误，断言统一 `403/settings_forbidden` 与无副作用。
- [x] 4.4 增加持久化失败与环境覆盖回归，断言环境变量存在时 `.env` 字节/mtime 不变，越过背压或文件不安全返回既有 `settings_update_invalid`/`settings_update_failed`，成功更新只返回待重启状态。
- [x] 4.5 增加旧 import/path 兼容回归，验证 `from api import ConcurrencyUpdateRequest`、既有 handler/helper import、canonical/legacy 请求和 `/config` 脱敏行为继续通过。
- [x] 4.6 运行与风险相称的现有 `tests/test_api.py`、`tests/test_config.py`、`tests/test_agent_runtime.py`、scope/安全相关测试及新增 Settings 测试，确认其它 HTTP 域无回归。

## 5. 严格验收与对抗性审查

- [x] 5.1 运行 `openspec validate refactor-core-http-settings-boundary --strict`，修复所有 artifact、delta spec、中文文档和路径问题后重新验证。
- [x] 5.2 将完成的实现交给 `luna_max` 做一次对抗性 review，重点检查循环依赖、路由漏注册/重复注册、鉴权优先级、恒定时间比较、环境覆盖 fail-closed、脱敏字段、原子写入和测试遗漏。
- [x] 5.3 根据 Luna review 结果修复所有 P1/P2 问题或记录有证据的残余风险，重新运行相关测试、静态依赖检查和严格 OpenSpec 校验；未完成 review 前不得宣称该 change 已实现。
