# 本地工具清单

本清单服务于表情包图片检索，不是项目运行的完整依赖。优先使用已安装工具；不要为了 Skill 安装浏览器、无头自动化框架或额外的 HTTP 客户端库。

## 必需

| 工具 | 用途 | Ubuntu 包 |
|---|---|---|
| `curl` | 调用 SerpApi 和下载候选资源 | `curl` |
| `jq` | 校验并提取 API JSON 字段，例如 `image_id` | `jq` |
| `file` | 在上传前确认 MIME 类型 | `file` |

## 建议

| 工具 | 用途 | Ubuntu 包 |
|---|---|---|
| `identify`、`magick` 或 `convert` | 查看尺寸，生成裁剪图或将本地图片压缩到 SerpApi 的 500 KB 限制内 | `imagemagick` |
| `ffmpeg` | 从 GIF、WebP 动图或视频中抽取代表帧 | `ffmpeg` |

## 按需

| 工具 | 用途 | Ubuntu 包 |
|---|---|---|
| `tesseract` | 在 VLM 不可用或 OCR 有争议时进行本地文字识别 | `tesseract-ocr` 及所需语言包 |
| `exiftool` | 检查或移除上传前图片的 EXIF 元数据 | `libimage-exiftool-perl` |

中文、日文、韩文和英文常用 OCR 语言包为 `tesseract-ocr-chi-sim`、`tesseract-ocr-chi-tra`、`tesseract-ocr-jpn`、`tesseract-ocr-kor`、`tesseract-ocr-eng`。

## 安装与检查

```bash
sudo apt-get update
sudo apt-get install -y jq imagemagick tesseract-ocr \
  tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
  tesseract-ocr-jpn tesseract-ocr-kor libimage-exiftool-perl

for tool in curl jq file identify ffmpeg tesseract exiftool; do
  command -v "$tool" >/dev/null && printf '%s: ok\n' "$tool" || printf '%s: missing\n' "$tool"
done
command -v magick >/dev/null && printf 'magick: ok\n' || command -v convert >/dev/null && printf 'convert: ok\n' || printf 'ImageMagick: missing\n'
```

Ubuntu 22.04 通常安装 ImageMagick 6，命令为 `convert`；ImageMagick 7 才通常提供 `magick`。项目已使用 Python、Pillow 与 `uv`；不需要为本 Skill 另行安装 Node HTTP 库、Python `requests`、Playwright 或浏览器。只有要处理动画媒体时才需要 `ffmpeg`，只有要做本地 OCR 时才需要 Tesseract。
