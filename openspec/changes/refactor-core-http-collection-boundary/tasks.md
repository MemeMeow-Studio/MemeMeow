## 1. 基线与边界

- [x] 1.1 固定七个合集 CRUD/详情/成员 route 的 path、method、status、tags、顺序、query、响应和错误事实，并确认导入/导出边界与 Server route 覆盖关系。
- [x] 1.2 读取 `CollectionRepository`、metadata service、scope 装配和现有合集/API/安全测试，冻结分页、名称、成员幂等和文件状态投影边界。

## 2. 合集 HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/collection_http.py`，模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 environment、metadata service 和 error callback 实现列表、创建、详情、重命名、删除和成员增删编排，保持 scope 查询和异常映射顺序。
- [x] 2.3 在 `api.py` 删除上述 route 的重复业务实现，保留 DTO、route decorator/query 声明、旧 handler 名称和兼容 helper；导入/导出实现保持原位置。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、scope/query 拒绝、CRUD/详情投影、名称冲突、成员原子/幂等和错误映射测试。
- [x] 3.2 运行合集/API/scope/security 定向测试、compileall 和 diff check，按失败修复并记录未运行的外部门禁。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和测试事实。

## 4. 最终验证与同步

- [x] 4.1 运行本 change OpenSpec strict validate 和开源完整回归，确认导入/导出 route 未重复、Server 覆盖边界未被改动。
- [x] 4.2 进行对抗性复核：检查客户端 scope/user 字段、跨 scope 资源、成员批次部分写入、路径泄露和导出覆盖；修复 P1/P2 或记录风险。
- [ ] 4.3 在开源仓库提交精确实现 SHA 与验证记录 SHA，停在用户审核门禁，未经批准不 fetch/merge 到 Server。
