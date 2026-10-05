# 检索图片复制与相册保存尺寸

检索页面的“复制尺寸”或“保存尺寸”提供原图、最长边 240px、360px、480px、640px。默认使用原图。尺寸设置由当前网站的 `localStorage` 保存，刷新页面和重新打开浏览器后继续使用；不同浏览器分别保存。

固定尺寸保持图片比例，不裁剪，不放大小图。例如 960×480 的图片选择最长边 480px 后得到 480×240。预览图片与检索结果缩略图保持各自现有尺寸。

网页复制写入 PNG，GIF 复制使用浏览器解码的静态画面。Android 相册保存保留 PNG、JPEG、GIF 格式；GIF 保留动画帧、显示时间、循环设置和透明背景。

媒体地址 `/media/{meme_id}?max_edge=480` 只接受上述四个数字档位，读取与原图相同的 scope 权限。尺寸输出使用已有派生文件缓存和进程级图片生成并发预算，以原图身份和尺寸区分缓存。原图删除时同时清理所有尺寸文件。GIF 使用 Docker 镜像已有的 gifsicle。固定尺寸输出受到内存、时间和 10 MiB 文件大小限制，小于所选档位的原图也检查输出文件大小；超限直接返回错误码和原因。

浏览器保存的尺寸无效时，页面显示错误并要求重新选择尺寸，图片操作会保留明确错误原因。重新选择后覆盖无效设置。

验证入口：`uv run pytest tests/test_image_export.py --basetemp=.local-real-test/pytest-image-export`；数据库测试需要显式设置开发测试数据库连接 `MEMEMEOW_TEST_DATABASE_URL`。前端使用 `npm test --prefix frontend -- src/utils/imageExportPreferences.test.ts src/utils/imageHttpError.test.ts`、`npm run typecheck --prefix frontend` 和 `npm run build --prefix frontend`。
