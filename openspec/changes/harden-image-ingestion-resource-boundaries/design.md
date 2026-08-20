## Context

上传入口和合集导入都在业务层持有完整图片字节，但图片解码限制分散在路由和资源包模块中。ZIP 预检已经有 500 成员和 512 MiB 解压总量，却没有压缩包原始大小、压缩放大比、ZIP64 和完整条目类型约束。导出目前以 `stat()` 后再 `read_bytes()` 读取，路径可能在两次系统调用之间被替换。

## Goals / Non-Goals

**Goals:**

- 让所有图片入口共享一套可终止的实际格式和资源预检。
- 在 ZIP 解压前限制压缩资源放大，并拒绝无法安全表达为普通图片的条目。
- 保持现有 v1 manifest、逐文件部分成功和幂等上传行为；失败预检不产生 durable 副作用。
- 让导出读取针对 scope 根目录执行 no-follow 文件打开，并保证临时文件/目录清理可重复且不跟随链接。

**Non-Goals:**

- 不修改数据库模型、图片处理状态机、账户 quota 或 HTTP 在途预算。
- 不引入新的归档格式或嵌套归档支持。
- 不把预检限制宣称为真实部署层的 Nginx、容器或模型资源隔离。

## Decisions

### 1. 共享图片预检使用独立模块和受控子进程

`backend/image_safety.py` 保存公开常量和 `validate_image_content()`。子进程重新打开输入字节，先检查实际格式、帧数和每帧/累计像素，再逐帧加载并验证；父进程以 10 秒截止时间等待，超时就终止并回收子进程。这样解码器即使卡在 Pillow 内部也不会继续占用服务进程；失败只返回稳定错误码，不把第三方异常传给 API。

### 2. ZIP 在中央目录阶段执行所有可廉价验证

`preflight_archive()` 先检查原始压缩包大小，再读取中央目录，拒绝 ZIP64 版本、重复/目录/链接/特殊条目，并按 `file_size / compress_size` 检查每项 100:1 放大比。之后才解析 manifest 和读取图片；读取后继续核对声明大小、SHA-256、嵌套归档和共享图片预检。

### 3. 导出使用 `O_NOFOLLOW` 和打开文件描述符

导出成员先由 `BlobStore.resolve()` 验证 scope 边界，再用 `os.open(..., O_NOFOLLOW)` 打开；`fstat()` 确认普通文件和大小，随后从同一描述符读取并复核长度/哈希。攻击者替换路径不会改变已经打开的对象，平台缺少 `O_NOFOLLOW` 时仍执行符号链接和普通文件检查。清理函数先单独处理符号链接，再对受控临时路径执行 unlink/rmtree，并将清理失败降级为可重复的 best-effort。

### 4. 上传归档入口使用有界 multipart 读取

合集导入复用现有 `_parse_upload_form()`，按 64 MiB 文件字节预算在 parser 返回前拒绝超限内容；业务层不使用无界 `UploadFile.read()`。直接调用 `preflight_archive()` 仍保留 64 MiB 二次检查，防止绕过 HTTP 入口。

## Error Contract

图片错误码包括 `invalid_image`、`unsupported_format`、`image_frame_count_exceeded`、`image_frame_pixels_exceeded`、`image_total_pixels_exceeded` 和 `image_preflight_timeout`。归档错误码包括 `archive_too_large`、`zip64_not_supported`、`compression_ratio_exceeded`、`nested_archive`、`unsafe_zip_entry`、`size_mismatch` 及既有 manifest/哈希错误；API 继续将它们映射为稳定中文消息。

## Testing Strategy

- 共享预检测试覆盖合法静态图、单帧/累计像素、100/101 帧、扩展名伪造、损坏图片和模拟超时进程回收。
- 上传契约测试确认资源预检发生在幂等查询和处理提交之前，超限图片不调用模型且不写入文件。
- ZIP 测试构造高放大比、ZIP64、嵌套、目录/符号链接/特殊文件、中央目录或 manifest 大小不一致和归档上限输入。
- 导出测试在成员路径替换为符号链接时确认拒绝，清理测试确认临时文件和目录删除且不跟随链接。
