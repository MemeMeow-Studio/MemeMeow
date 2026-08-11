# SerpApi Google Lens 图片检索

仅当任务允许使用 SerpApi 且运行环境已设置 `SERPAPI_API_KEY` 时，使用本说明。项目的本地配置位置是 `.env`，其模板为 `.env.example`；执行环境必须自行导出该变量，不能假设任意 Agent 或 shell 会自动加载 `.env`。密钥只能从环境变量或密钥管理器读取，不能写入代码、Skill、查询日志或最终输出。需要准备命令行工具时，读取 [本地工具清单](local-tooling.md)。

SerpApi 返回的是 Google Lens 的候选网页和图片，不是对表情包含义或出处的结论。它最有价值的作用是为未知角色、模板或普通配文补齐文字检索锚点：先观察图片，再用候选的标题、链接和图片进行核验，最后才将确认过的名称和短语交给文字搜索。

## 选择输入

- **公开图片 URL**：直接传 `url`。URL 必须能被 SerpApi 从公网访问。
- **本地图片**：先上传，取得临时 `image_id`，然后立刻检索。上传只支持 JPG/JPEG、PNG、WebP，最大 500 KB；`image_id` 10 分钟后失效。图片过大时生成临时缩小副本，绝不修改原图。
- 先检索整图。只有整图结果不足以识别主体、模板或引用时，才检索有区分度的裁剪图，例如去掉后加文字的主体区域。每个额外查询都应对应一个尚未解决的证据问题；命中缓存时不要重复调用供应商。

## 调用

### 推荐：使用带持久化缓存的工作区脚本

对本地图片优先使用脚本，而不是直接重复执行 curl。默认缓存目录为
`data/reverse_image_cache/serpapi_google_lens/`，也可以用
`MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT` 或 `--cache-root` 覆盖。

```bash
uv run python .agents/skills/research-meme-context/scripts/serpapi_google_lens.py \
  /home/infstellar/vscode/MemeMeow/example_meme_1.jpg
```

脚本输出包含 `cache.status`：首次为 `miss`，复用已有成功快照为 `hit`，使用
`--refresh` 强制请求并追加新快照。成功结果默认长期复用；空结果只复用 3 天；网络或
供应商错误不会写入可复用快照。

脚本会自动从工作区 `.env` 读取 `SERPAPI_API_KEY`。输出结果已经移除 `image_id`、SerpApi
归档地址和其他内部标识，但会保留候选网页及图片的公开链接供 Agent 核验。

### 手动调试：公开 URL

这条命令不经过工作区持久化缓存。需要复用结果时，先将公开图片下载为本地文件，再使用上面的缓存脚本。

```bash
curl --fail-with-body --silent --show-error --get 'https://serpapi.com/search.json' \
  --data-urlencode 'engine=google_lens' \
  --data-urlencode "url=$IMAGE_URL" \
  --data-urlencode 'type=all' \
  --data-urlencode 'hl=zh-cn' \
  --data-urlencode "api_key=$SERPAPI_API_KEY"
```

### 手动调试：本地图片

仅在诊断上传或 API 参数时使用；正常研究任务使用缓存脚本。

```bash
upload_json=$(curl --fail-with-body --silent --show-error -X POST 'https://serpapi.com/image' \
  -F "image=@$IMAGE_PATH" \
  -F "api_key=$SERPAPI_API_KEY")

image_id=$(jq -er '.image_id // error(.error // "SerpApi 图片上传失败")' <<<"$upload_json")

curl --fail-with-body --silent --show-error --get 'https://serpapi.com/search.json' \
  --data-urlencode 'engine=google_lens' \
  --data-urlencode "image_id=$image_id" \
  --data-urlencode 'type=all' \
  --data-urlencode 'hl=zh-cn' \
  --data-urlencode "api_key=$SERPAPI_API_KEY"
```

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
