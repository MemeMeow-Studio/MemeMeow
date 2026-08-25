## 1. 基线与规划

- [x] 1.1 记录开源与 Server 工作区状态、SearchRepository 实现范围、兼容导出和不触碰的周边边界。
- [x] 1.2 完成 proposal、spec、design 与回滚路径，并通过 OpenSpec 规划校验。

## 2. SearchRepository 实现边界

- [x] 2.1 新增带中文模块/class/function docstring 的 `backend/persistence/repositories/search.py`，按原顺序移动 SearchRepository 完整实现和最小依赖。
- [x] 2.2 修改 `backend/database.py` 删除重复 SearchRepository class 并显式 re-export 新类；保留 VisualEmbeddingRepository、TaskRepository、ReverseImageUsageRepository、BlobStore、StorageCoordinator 及其余行为。
- [x] 2.3 确认 `DataEnvironment` 仍通过原 facade lazy assembly 共享同一 Session 和 scope，新模块不在顶层导入 `backend.database`，Repository 包入口只提供单一实现。

## 3. 契约测试与验证文档

- [x] 3.1 增加类身份、唯一实现来源、依赖方向、资源装配和周边边界契约测试。
- [x] 3.2 增加/更新 generation/head、migration epoch、legacy/incremental source dispatch、embedding 错误和稳定排序回归测试。
- [x] 3.3 运行开源目标测试、PostgreSQL marker、完整回归、OpenSpec strict、compileall、`git diff --check`，新增 validation.md 记录实际结果和未运行门禁。

## 4. 提交、精确同步与 Server 收尾

- [x] 4.1 在开源仓库提交实现、验证和收尾 artifacts，固定精确实现/验证/收尾 SHA；确认不访问 upstream、不 push。
- [ ] 4.2 检查 Server 目标路径无重叠脏改动，从本地精确 fetch 开源收尾 SHA，以普通 `--no-ff` merge 进入 Server `main`，确认开源收尾 SHA 是 Server `HEAD` 真实祖先。
- [ ] 4.3 在 Server 运行目标/完整回归和静态门禁，更新本 change validation 与 `docs/refactor-plan.md`，记录 Server merge SHA、祖先关系、验证结果、未配置 PostgreSQL/未运行 Compose 事实和回滚方式；保持其它脏文件不变。
