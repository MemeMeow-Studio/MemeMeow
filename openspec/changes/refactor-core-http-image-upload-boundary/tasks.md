## 1. 基线与 OpenSpec

- [x] 1.1 固定 `/images/upload` 的 route 元数据、multipart 字段、请求/单文件预算、响应 key、错误 code、scope/policy 和任务副作用顺序，确认合集导入只复用 parser 不被本切片接管。
- [x] 1.2 读取上传资源边界、图片安全、metadata/blob、operation policy、处理 worker 及现有上传/API/security 测试，记录开源与 Server 适配差异和回滚点。

## 2. Image upload HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_upload_http.py`，移动有界 multipart parser、逐 spool 读取和幂等结果 helper；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service、校验、processing、operation、错误和检索 callback 实现上传编排，保持逐文件顺序、scope 绑定、fail-closed、幂等复用、部分成功及 Server 可配置 policy/release 语义。
- [x] 2.3 在 `api.py` 删除上传 route 的重复业务实现，保留 canonical decorator、旧 `upload_images`/parser/read/idempotent helper 名称和合集导入兼容调用；不得重复注册 route。

## 3. 契约测试与文档

- [x] 3.1 增加 route/dependency snapshot、multipart 总预算/单文件边界、未知字段、scope/policy 拒绝、幂等和 durable 副作用顺序测试。
- [x] 3.2 运行上传/API/scope/security 定向测试、compileall、diff check，并按失败修复；记录 PostgreSQL marker 的 skipped/未配置说明。
- [x] 3.3 更新 change README、tasks 和验证记录，明确开源实现 SHA、验证/收尾 SHA、范围与对抗性复核结论。

## 4. 最终验证与同步

- [x] 4.1 运行本 change OpenSpec strict validate 和开源全套或明确范围回归，确认其它图片/合集 route 未重复或改变。
- [x] 4.2 检查文件路径/symlink/race、scope/auth fail-closed、operation quota 收束、处理任务失败诊断和兼容 alias；修复所有 P1/P2 风险或记录可接受残余风险。
- [ ] 4.3 检查 Server 工作区状态后从精确开源实现/验证/收尾 SHA fetch，普通 `--no-ff` merge 并验证祖先关系；运行 Server 定向回归，记录 merge SHA、回滚点和既有 OpenSpec 失败。
