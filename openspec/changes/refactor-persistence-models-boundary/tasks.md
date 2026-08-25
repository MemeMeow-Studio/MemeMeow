## 1. 基线与设计

- [x] 1.1 记录 `backend/database.py` 的模型声明范围、旧导出清单、metadata 表集合和当前 Server/开源工作区状态。
- [x] 1.2 完成 proposal、design、spec 与回滚路径，确认不修改 schema、migration、Repository、BlobStore、StorageCoordinator 或其它 active change。

## 2. ORM 模型边界

- [x] 2.1 新增带中文模块/类/函数 docstring 的 `backend/persistence` 包和 `models.py`，按原顺序移动 Base、ScopeContext、全部 ORM 类、索引和 optional control table 集合。
- [x] 2.2 让 `backend/database.py` 通过显式 re-export 复用 models 对象，保持旧模型、常量、`utcnow` 和 optional table 导入兼容，并移除重复 ORM 声明。
- [x] 2.3 静态核验 models 不反向导入 `backend.database`，且 Repository、UnitOfWork、BlobStore、StorageCoordinator 和资源装配代码仍在 database.py 原职责范围。

## 3. 契约测试与验证

- [x] 3.1 增加模型身份、metadata 单一来源、表/约束/索引和 backend.database 兼容导出的契约测试。
- [x] 3.2 运行持久化/API/scope/任务相关目标测试，并根据失败修复导入兼容问题。
- [x] 3.3 运行开源完整回归、OpenSpec strict validate、compileall 和 `git diff --check`，记录 PostgreSQL/Compose 是否实际运行及既有 OpenSpec 失败。

## 4. 提交、同步与收尾

- [ ] 4.1 在开源仓库提交实现 SHA、验证记录 SHA 和收尾 SHA，固定变更范围、验证结果与回滚方式。
- [ ] 4.2 检查 Server 工作区重叠脏改动后，从本地开源精确 fetch 收尾 SHA 并普通 `--no-ff` merge，确认开源 SHA 是 Server HEAD 真实祖先。
- [ ] 4.3 运行 Server 定向/完整回归和静态门禁，更新 `docs/refactor-plan.md`，记录 merge SHA、祖先关系、风险与未运行环境门禁。
