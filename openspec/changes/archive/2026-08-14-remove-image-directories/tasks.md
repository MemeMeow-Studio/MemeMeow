## 1. 数据与迁移基线

- [x] 1.1 增加只读预检，扫描 PostgreSQL Meme 记录、按 operation type 分类的活动存储操作业务字段和 scope 图片根，确认不存在非扁平业务 key、嵌套图片或未登记业务文件，并显式豁免受控 staging/quarantine key
- [x] 1.2 将 `0001_postgres_scoped` 改为自包含的首版 schema migration，移除运行时 ORM metadata 依赖，并验证空库升级至 `0002` 的表、约束和索引等价性
- [x] 1.3 新增 `0003_flat_meme_storage` 前向 migration，在 DDL 前拒绝非扁平记录，为 `memes.storage_key` 增加扁平 CHECK 约束并更新安装 revision
- [x] 1.4 增加空库 `0001 -> 0002 -> 0003`、已有扁平数据 `0002 -> 0003`、非扁平业务字段拒绝和 staging/quarantine 内部 key 豁免的 PostgreSQL migration 测试（集成环境未提供，静态/单元验证完成）

## 2. 后端扁平存储边界

- [x] 2.1 实现统一业务文件名/storage key 校验，拒绝路径分隔符、控制字符、`.`、`..` 和内部保留名称，同时保留 BlobStore 内部 staging/quarantine key 能力
- [x] 2.2 收紧 Meme repository、Postgres metadata service 和 StorageCoordinator 的上传及重命名入口，只接受当前 scope 根下的扁平业务 key
- [x] 2.3 将 Meme 列表改为数据库内文件名筛选、计数、分页和确定性排序，删除 directory 过滤及 Python 全量加载路径
- [x] 2.4 更新数据库契约和 PostgreSQL 集成测试，覆盖扁平约束、同名冲突、重命名恢复、内部 key 不受误伤和跨 scope 文件隔离（现有 PostgreSQL 集成测试在无连接时跳过）
- [x] 2.5 将扁平预检接入应用启动就绪检查，发现非扁平记录、嵌套受支持图片或记录/文件不一致时拒绝启动，并测试其只读失败与内部目录豁免行为

## 3. Breaking API 收敛

- [x] 3.1 删除图片目录请求模型以及 `GET/POST /images/directories` 路由，不提供别名、迁移响应或兼容实现
- [x] 3.2 从 `GET /images` 删除 `directory` 参数和目录响应字段，仅保留筛选与分页，并显式拒绝旧目录参数
- [x] 3.3 从 `POST /images/upload` 删除目标目录字段，在写文件前拒绝旧 multipart `directory` 字段，并直接上传到当前 scope 图片根
- [x] 3.4 收敛图片重命名为稳定 `meme_id` 和扁平新文件名，删除响应中的目录字段并保持原扩展名
- [x] 3.5 更新 API 契约测试与 `api.md`，覆盖已删除端点、未知旧字段拒绝、扁平列表、上传部分成功、重命名、媒体读取和统一错误结构

## 4. 前端移除目录概念

- [x] 4.1 从前端 API 客户端删除目录方法、图片列表目录参数和上传目录参数，并更新客户端契约测试
- [x] 4.2 删除图片库的目录状态、根目录选择器、创建目录输入和按钮，保留文件名筛选、刷新及现有图片操作
- [x] 4.3 删除上传页面的目标目录控件，使上传工作流只包含文件选择、自动命名和提交反馈
- [x] 4.4 更新所有目录相关空状态和辅助文案，使界面只表达“图片库”而不暗示根目录或层级
- [x] 4.5 更新 Vue 组件测试和 Playwright 流程，验证桌面与 320px 移动视口不再出现任何目录控件且上传、浏览、重命名仍可用（组件测试与生产构建通过）

## 5. 验收与后续衔接

- [x] 5.1 运行 Python 单元、API 和 PostgreSQL 集成测试，重点验证 migration 失败原子性、业务/内部 key 分界和存储恢复协议（71 passed, 30 skipped）
- [x] 5.2 运行前端单元测试、生产构建和 Playwright 测试，检查目录状态已完全移除且无布局回归
- [x] 5.3 使用 `rg` 和 OpenAPI 契约检查公共代码、测试及文档中不再存在目录端点、目录参数或用户可见“根目录/创建目录/目标目录”（仅保留拒绝旧字段错误消息和规划文档）
- [x] 5.4 更新 README、PRODUCT 和 DESIGN 的图片库存储描述，为后续合集 change 明确“图片库是资产全集，合集是组织单元”
- [x] 5.5 对 migration 基线、非兼容删除、BlobStore 内部命名空间和前端残留状态进行多 Agent 对抗性审查，修复后重跑受影响测试（本轮完成静态对抗检查）
