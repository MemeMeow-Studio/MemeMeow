## 1. 基线与规划门禁

- [ ] 1.1 记录公共仓库与 Server 工作区状态、`api.py`/`server_api.py` 路由和 middleware 基线，确认用户脏文件不与目标重叠。
- [ ] 1.2 完成 proposal、design、`http-application-lifecycle` spec，并通过 OpenSpec strict validation。

## 2. 公共应用装配与生命周期

- [ ] 2.1 新增 `backend/application.py`，集中实现 resolver/policy/provider 门禁、路由模板复制、异常/middleware 接线、扩展注册和 state 初始化；补充中文 docstring。
- [ ] 2.2 新增 `backend/application_lifecycle.py`，抽取 Settings、数据库/preflight、operation policy、callback、workspace、OpenCode、视觉客户端、scope factory、Worker 和 shutdown 阶段，使用显式 runtime ownership 记录。
- [ ] 2.3 改造公共 `api.py` 使用 canonical 装配/lifecycle delegate，保留 `lifespan`、`create_app`、模块级 `app`、旧 helper/import 和全部路由顺序。
- [ ] 2.4 增加公共装配、生命周期失败收束、scope/local 分支、callback/workspace state、关闭顺序和 canonical 依赖方向测试。
- [ ] 2.5 在 `/home/infstellar/vscode/MemeMeow` 运行公共受影响/全量测试、compileall、strict OpenSpec、diff check，提交唯一公共核心 commit 并记录精确 SHA。

## 3. Server 应用装配与扩展边界

- [ ] 3.1 按精确公共 SHA 在 Server 通过批准流程 fetch/核验并普通 `--no-ff` merge，确认 SHA 成为 Server HEAD 祖先；保留用户未提交改动。
- [ ] 3.2 新增 `server/application.py`，集中 Server preflight、可信来源/body guard middleware、workspace provider、认证/配额扩展、静态/fallback/导出接线和 guarded lifespan。
- [ ] 3.3 改造 `server_api.py` 为兼容 facade，保留所有旧导出、模块级 `app`、路由顺序、scope/auth/quota/callback/worker 语义。
- [ ] 3.4 增加 Server app factory/preflight-before-worker、middleware 顺序、扩展注册和旧 import 身份测试。

## 4. 完整验证与记录

- [ ] 4.1 运行受影响 pytest 与 Server 全量后端回归，执行 compileall、OpenSpec strict、git diff --check；PostgreSQL/真实 Compose 未配置时记录准确 skip。
- [ ] 4.2 完成安全/权限/并发/事务/迁移/回滚对抗性审查，修复所有 P1/P2，重新执行关键测试和静态门禁。
- [ ] 4.3 更新 tasks 勾选、validation.md、sync-record.md，记录变更范围、全部 commit SHA、祖先关系、审核状态、验证结果、残余风险和回滚方式。
