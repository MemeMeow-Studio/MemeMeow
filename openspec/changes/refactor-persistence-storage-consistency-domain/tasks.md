## 1. 基线与聚合 change

- [x] 1.1 记录 Server/开源工作区状态、现有 storage 实现、已完成 persistence changes、active changes、schema/migration 和旧 import 清单。
- [x] 1.2 完成 proposal、design、storage consistency spec 和回滚/同步门禁，确认本域不再拆分为多个 change。

## 2. Canonical storage 边界

- [x] 2.1 新增带中文模块、类、函数 docstring 的 `backend/persistence/storage.py`，移动 BlobStore/StorageCoordinator 全部实现及依赖，保持路径安全、fsync、状态矩阵、恢复/补偿、durable fact 和 claim fencing 语义。
- [x] 2.2 更新 `backend/persistence/resources.py` 直接装配 canonical storage，消除 resources 对 facade storage symbol 的运行时依赖并保持 scope namespace/生命周期行为。
- [x] 2.3 将 `backend/database.py` 收敛为显式兼容 facade，保留 BlobStore、StorageCoordinator 和既有模型/Repository/engine/UoW/常量/error 导出；确保无重复实现和无循环导入。

## 3. 契约与失败恢复测试

- [x] 3.1 增加 canonical/facade 对象身份、AST 依赖方向、旧 import 和 metadata/storage operation schema 契约测试。
- [x] 3.2 增加 BlobStore 路径、符号链接、暂存、原子不覆盖移动、删除/隔离和权限边界失败测试。
- [x] 3.3 增加 StorageCoordinator 上传/rename/delete 的 durable intent、失败补偿、恢复、ambiguous/blocked、CAS lease/owner/revision/title 指纹和未知执行测试；验证 grant/lease 释放不跨越 durable/unknown 边界。

## 4. 验证、提交与 Server 同步

- [x] 4.1 运行开源定向回归、完整回归、OpenSpec strict、compileall、`git diff --check`，真实 PostgreSQL/Compose 未配置时明确 skip，并写入本 change validation.md。
- [x] 4.2 进行一次严格对抗性 review，覆盖安全、权限、事务、并发、迁移、回滚、恢复和测试覆盖；修复所有 P1/P2 并再次复审，保持 artifacts/tasks 一致。
- [x] 4.3 在开源仓库提交聚合实现、验证和收尾 SHA，记录精确 SHA、变更范围、验证和审核状态；用户明确授权后从本地精确 fetch 收尾 SHA，不访问 upstream、不 push。
- [ ] 4.4 检查 Server 重叠脏改动后，在 Server `main` 通过普通 `--no-ff` merge 同一精确开源 SHA；运行 Server 定向回归、strict validate、compileall、`git diff --check`，记录 merge SHA、祖先关系、变更范围、未运行门禁与残余风险。
