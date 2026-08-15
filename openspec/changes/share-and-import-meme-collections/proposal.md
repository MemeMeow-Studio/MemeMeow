## Why

MemeMeow 已支持在本地图片库中组织表情包合集，但合集目前无法离开当前实例。使用者需要一个简单的 ZIP 下载链接来获取合集当前内容，并能把同一格式的 ZIP 导入另一个实例或重新导入当前实例。

## What Changes

- 为每个合集提供稳定的公开下载地址；请求该地址时实时读取数据库中的合集名称和当前成员，并现场构造 ZIP，不保存导出快照或分享记录。
- 定义版本化的 MemeMeow 合集 ZIP 格式，包含 `manifest.json` 和图片文件，并使用 SHA-256 校验文件完整性。
- 新增合集 ZIP 导入接口，在完整校验包结构后创建合集、导入图片并建立成员关系。
- 对导入中的合集重名返回冲突；对同名同内容图片复用现有 Meme，对同名不同内容图片使用哈希后缀生成安全文件名。
- 导入图片沿用普通新图片的语境处理和 embedding 重建流程，不导入 embedding、任务状态或数据库内部标识。
- 在合集界面增加下载 ZIP、复制下载链接和导入合集入口。
- 不新增用户、认证、分享 token、有效期、权限、分享记录或异步导出任务。

## Capabilities

### New Capabilities

- `meme-collection-packages`: 定义合集动态 ZIP 下载、版本化包格式、安全导入、冲突处理和前端操作流程。

### Modified Capabilities

无。

## Impact

- FastAPI 新增合集导出和导入端点，以及 ZIP manifest 的请求、响应和错误契约。
- 后端新增小型合集包服务，复用现有 scope-bound 合集 repository、`BlobStore`、`StorageCoordinator` 和图片校验逻辑。
- Vue 合集工作区新增下载、复制链接和上传 ZIP 的交互；二进制下载使用普通浏览器下载链接，不经过 JSON 请求封装。
- 增加 ZIP 格式、动态成员读取、文件冲突、损坏包、路径穿越、大小限制、部分导入结果和前端工作流测试。
- 不新增数据库表或 migration，不改变现有合集与 Meme 的持久化模型。
