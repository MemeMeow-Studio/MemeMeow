## 1. 基线与边界

- [x] 1.1 记录开源与 Server 工作区状态、`backend/database.py` 两个 Repository 的实现范围和兼容导出，确认不触碰 Search/Task/Blob/Storage/schema/migration 及 active change 脏文件。
- [x] 1.2 完成 proposal、spec、design 与回滚路径，锁定 scope、事务、SQL、错误码、分页、成员和导出快照不变。

## 2. Repository 实现边界

- [x] 2.1 新增带中文模块/类/函数 docstring 的 `backend/persistence/repositories` 包、`memes.py` 和 `collections.py`，按原顺序移动两个 Repository 的完整实现及最小依赖。
- [x] 2.2 修改 `backend/database.py` 删除两个重复 class 定义并显式 re-export 新模块类；保留 SearchRepository、TaskRepository、BlobStore、StorageCoordinator 和其余 imports/行为。
- [x] 2.3 检查新模块不在顶层导入 `backend.database`，`DataEnvironment` 仍通过原 facade lazy assembly 共享同一 Session 和 scope。

## 3. 契约测试与验证

- [x] 3.1 增加 Repository 类身份、唯一实现来源、依赖方向和资源装配契约测试。
- [x] 3.2 运行 Meme/合集/API/scope/存储相关目标测试，根据失败修复仅限导入兼容的缺陷。
- [x] 3.3 运行开源完整回归、PostgreSQL marker、OpenSpec strict、compileall 和 `git diff --check`，记录数据库/Compose 门禁与既有 OpenSpec 失败。

## 4. 提交、同步与收尾

- [ ] 4.1 在开源仓库提交实现、验证和收尾记录，固定精确 SHA、变更范围、验证结果、祖先关系和回滚方式。
- [ ] 4.2 检查 Server 目标路径无重叠脏改动，从本地开源精确 fetch 收尾 SHA，以普通 `--no-ff` merge 进入 Server `main`，确认精确 SHA 为 Server `HEAD` 祖先。
- [ ] 4.3 运行 Server 定向/完整回归及静态门禁，更新 `docs/refactor-plan.md`、本 change validation 和同步记录，确认 Server 未 push。
