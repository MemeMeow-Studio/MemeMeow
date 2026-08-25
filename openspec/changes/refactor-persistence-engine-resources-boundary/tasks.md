## 1. 基线与设计

- [x] 1.1 记录当前开源/Server 工作区状态、engine/UoW/resources 旧实现范围、facade 导出与
  active change 重叠检查；确认不修改 schema、migration、Repository、BlobStore、
  StorageCoordinator 或 HTTP。
- [x] 1.2 完成 proposal、design、spec，明确循环依赖规避、兼容导出、测试门禁和回滚顺序。

## 2. 持久化边界实现

- [x] 2.1 新增带中文模块/类/函数 docstring 的 `backend/persistence/engine.py`，移动
  `DatabaseError` 与六个 engine/初始化函数，保持 SQL、错误码和参数事实。
- [x] 2.2 新增 `backend/persistence/unit_of_work.py`，移动 `UnitOfWork` 并保持提交、回滚、
  关闭和 scope 校验语义。
- [x] 2.3 新增 `backend/persistence/resources.py`，移动 `DataEnvironment` 与
  `DatabaseResources`，通过安全的延迟导入继续组装现有 Repository、BlobStore 和
  StorageCoordinator。
- [x] 2.4 修改 `backend/database.py` 删除重复实现并显式 re-export 新模块对象；确保其余
  Repository、BlobStore、StorageCoordinator 代码和 imports 事实不变。

## 3. 契约测试与验证

- [x] 3.1 增加 engine、UoW、resources 单一实现来源、facade identity、依赖方向和生命周期
  契约测试。
- [ ] 3.2 运行持久化、scope、任务、存储、API 相关目标测试，并根据失败修复导入兼容问题。
- [ ] 3.3 运行开源相关全量回归、OpenSpec strict validate、compileall 与 `git diff --check`；
  记录 PostgreSQL/Compose 是否实际运行及既有失败/skip 原因。

## 4. 提交、同步与收尾

- [ ] 4.1 在开源仓库提交实现、验证和收尾记录的精确 SHA，固定变更范围、验证结果、祖先
  关系和回滚方式。
- [ ] 4.2 检查 Server 工作区是否存在目标重叠脏改动；在无冲突且已获授权时精确 fetch 开源
  收尾 SHA，以普通 `--no-ff` merge 进入 Server `main`，不得绕过历史。
- [ ] 4.3 运行 Server 定向/全量回归和静态门禁，记录 merge SHA、开源 SHA 祖先关系、
  PostgreSQL/Compose 未运行门禁和回滚方式。
