# SerpApi Google Lens 图片检索

仅当任务 payload 的 `reverse_image_policy` 为 `auto` 时，使用本说明。Agent 只能调用项目薄 CLI；后端从服务端配置读取供应商密钥，Runner 运行时向 Agent 提供 `MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL`、当前 `MEMEMEOW_AGENT_TASK_ID` 和绑定当前 claim 的 `MEMEMEOW_AGENT_CALLBACK_TOKEN`。不要读取 `.env`，不要设置或检查 `SERPAPI_API_KEY`。需要准备命令行工具时，读取 [本地工具清单](local-tooling.md)。

SerpApi 返回的是 Google Lens 的候选网页和图片，不是对表情包含义或出处的结论。它最有价值的作用是为未知角色、模板或普通配文补齐文字检索锚点：先观察图片，再用候选的标题、链接和图片进行核验，最后才将确认过的名称和短语交给文字搜索。

## 选择输入

- **公开图片 URL**：直接传 `url`。URL 必须能被 SerpApi 从公网访问。
- **本地图片**：通过项目薄客户端提交图片，由后端负责供应商上传和临时标识生命周期。客户端只需提供 JPG/JPEG、PNG、WebP 图片，最大 500 KB；图片过大时生成临时缩小副本，绝不修改原图。
- 先检索整图。只有整图结果不足以识别主体、模板或引用时，才检索有区分度的裁剪图，例如去掉后加文字的主体区域。每个额外查询都应对应一个尚未解决的证据问题；命中缓存时不要重复调用供应商。

## 调用

### 使用项目内部薄客户端

对本地图片使用薄客户端。缓存、同键并发锁、供应商访问和 usage event 都由后端
`/internal/reverse-image/search` 统一处理；客户端不会读取供应商密钥，也不会写入本地生产缓存。

```bash
python skills/research-meme-context/scripts/serpapi_google_lens.py \
  /images/example_meme_1.jpg --task-id "$MEMEMEOW_AGENT_TASK_ID"
```

脚本输出包含 `cache.status`：首次为 `miss`，复用已有快照为 `hit`，使用
`--refresh` 强制请求。成功结果默认长期复用；空结果只复用 3 天；网络或供应商错误
不会写入可复用快照。脚本默认省略 `request_id`，服务端返回权威 ID；仍可用
`--request-id` 兼容旧脚本。相同当前 claim、规范化参数和 `refresh` 的重试会复用同一
callback/usage 事实，provider 已开始但结果未知时返回 `reverse_image_unknown_execution`
且不会自动重放。

脚本只调用携带当前任务 callback 凭据的内部 multipart 接口并输出统一 JSON；缓存、供应商访问、脱敏和 usage event 由后端负责。输出结果不包含 `image_id`、SerpApi 归档地址和其他内部标识，但会保留候选网页及图片的公开链接供 Agent 核验。

### 手动调试：公开 URL

生产 Agent 不得使用以下供应商直连调试命令；它们仅作为后端适配器开发参考。

供应商直连调试命令不属于 Agent 契约；后端适配器开发应使用离线 fake provider 或受控测试环境，避免在 Skill 文档、日志或任务环境中出现密钥和 `image_id`。

参数参考：`type=all`、`hl`、`country`、`q` 和 `auto_crop` 由薄客户端提交，后端负责供应商映射。

<!-- 供应商直连的公开 URL 和本地上传示例已移除；Agent 只能使用上面的内部接口薄客户端。 -->
### 供应商参数参考

仅描述参数语义，不提供可绕过后端边界的供应商调用命令。

`type=all` 是首轮默认值。只有首轮有具体缺口时才追加一次：

- `type=exact_matches`：寻找相同或近乎相同的图片、早期转载和模板来源；
- `type=visual_matches`：寻找同一角色、构图或二创变体；
- `q=<已观察到的独特短语或主体特征>`：仅用于已有观察依据的细化，不要用模型臆测的人名或梗义污染首轮检索；
- `auto_crop=true`：仅当要验证图片中明确可见的主体区域时使用。手动裁剪通常更可控。

可按目标受众设置 `hl` 和 `country`，但同一研究中的语言和地区应保持一致。不要默认使用 `no_cache=true`；相同参数的一小时缓存结果免费且不计入配额。一般不使用 `async=true`，它需要再通过 Searches Archive API 轮询结果，且不能和 `no_cache` 同时使用。

## 读取与核验结果

1. 先检查 `search_metadata.status`。只有 `Success` 才读取候选；`Error` 或顶层 `error` 说明请求失败。
2. 优先读取 `visual_matches`。每项通常包括 `title`、`link`、`source`、`thumbnail`，有时还有原图 `image`、尺寸和通向精确匹配的链接。字段可能缺失，不能假设固定存在。
3. 视结果读取 `exact_matches`、`related_content`、`knowledge_graph`、`organic_results` 或 `short_videos`。它们是补充候选，不保证每次返回。
4. 对排名靠前且相关的候选，下载或打开页面比对主体、构图、文字和发布时间。搜索结果标题、缩略图、网页文件名都不能单独证明出处。
5. 将确认的名称、原文别名、作品名和出处线索写入 `keywords`、`search_queries`、`references`；无法确认的竞争性解释写入 `uncertainties`。

结果为空不代表图片没有网络出处。依次检查：图片是否被裁剪或压缩、是否混入了后加文字、`hl`/`country` 是否合适、以及是否应以一个主体裁剪图进行一次受限重试。

## 安全与调用策略

- 不传包含用户隐私、内部内容或无权发送给第三方的图片。
- 不记录或输出 `api_key`、`image_id`、完整上传响应或 SerpApi 搜索归档 URL。`image_id` 是短期传输凭据，不是表情包元数据。
- 不设置固定的每图请求上限。只要仍有明确、可验证且尚未解决的证据问题，就可以继续搜索；当连续结果不再增加信息时停止。优先检查缓存，并避免提交重复参数的请求。
- SerpApi 的 Google Lens 是第三方对 Google Lens 结果的封装。字段、召回结果和可用性可能变化；将空结果和候选结果都视为有限证据。

## 官方文档

- [Google Lens API](https://serpapi.com/google-lens-api)
- [上传图片后调用 Google Lens](https://serpapi.com/google-lens-upload-an-image)
- [Image API](https://serpapi.com/image-api)
