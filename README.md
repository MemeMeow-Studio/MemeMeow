<div align="center">

<pre align="center">
███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗███████╗ ██████╗ ██╗    ██╗
████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║██╔════╝██╔═══██╗██║    ██║
██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██║   ██║██║ █╗ ██║
██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║   ██║██║███╗██║
██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗╚██████╔╝╚███╔███╔╝
╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝ ╚═════╝  ╚══╝╚══╝ 
</pre>

_✨ 通过自然语言检索表情包 ✨_

[在线体验](https://zvv.quest) · [反馈问题](https://github.com/MemeMeow-Studio/MemeMeow/issues) · [参与贡献](https://github.com/MemeMeow-Studio/MemeMeow/pulls)

[![License](https://img.shields.io/github/license/MemeMeow-Studio/MemeMeow)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org)
[![Online Demo](https://img.shields.io/website?url=https%3A%2F%2Fzvv.quest&up_message=online&down_message=offline&label=demo)](https://zvv.quest)

---

<p align="center">
    <a href="#-features">Features</a> •
    <a href="#-screenshots">Screenshots</a> •
    <a href="#-quick-start">Quick Start</a> •
    <a href="#-usage">Usage</a> •
    <a href="#-api">API</a> •
    <a href="#-related-applications">Related Applications</a>
</p>

</div>

<a id="-features"></a>
## ✨ Features

> [!CAUTION]
> 本项目返回表情包结果由AI生成，与本人观点无关。

- **自然语言处理**: 采用嵌入模型，实现 Q&A 式的检索，能够对给出问题自动使用表情包回应。
- **高拓展性**: 可结合 VLM 高效为图片生成候选描述并人工确认文件名。
- **便捷使用**: 提供 Vue Web 界面、统一 API 和受控媒体访问，可部署在本地单机环境。
- **可维护**：长任务、缓存和文件边界都有明确的 API 状态与错误契约。
- 另外，**单纯使用检索功能**，若使用API无需任何花费💰


Mememeow 是一个基于自然语言的表情包检索工具。它能让你通过描述想要的场景，快速找到合适的表情包。不再需要记住具体的文件名或标签，就能轻松找到想要的表情！

<a id="-screenshots"></a>
## 📸 Screenshots

当前界面由 Vue 3 提供，启动后访问 `http://localhost:5275`。迁移前的 Streamlit 截图仍保存在 [`legacy/streamlit-v1/screenshots/`](legacy/streamlit-v1/screenshots/)，仅用于历史参考。

## ℹ️ Data Source

本项目张维为表情包来源于 [知乎](https://www.zhihu.com/question/656505859/answer/55843704436)

> [!CAUTION]
> 若有侵权，请联系删除

<a id="-quick-start"></a>
## 🚀 Quick Start

### 环境要求

- Python 3.12（使用 uv 管理）
- Node.js 22+
- 可选：嵌入模型 API Key、VLM API Key

### 安装步骤

1. 克隆仓库
```bash
git clone https://github.com/MemeMeow-Studio/MemeMeow.git
cd MemeMeow
```

2. 安装后端依赖
```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements-dev.txt
cp .env.example .env
```

3. 启动 FastAPI
```bash
uvicorn api:app --reload --port 8275
```

4. 启动 Vue 开发服务器（另一个终端）
```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

也可以使用项目根目录的 `./start.sh` 一键停止旧实例并在 tmux 中启动前后端。

<a id="-usage"></a>
## 📖 Usage

### Basic Usage

1. 访问 Web 界面（开发模式默认为 `http://localhost:5275`，后端默认为 `http://localhost:8275`）
2. 在搜索框中输入你想要的表情包场景描述
3. 点击搜索，系统会返回最匹配的表情包

### 图片管理

#### 上传新图片

1. 进入“上传”页面
2. 选择目标目录和图片文件
3. 可选：启用 VLM 自动命名

> [!CAUTION]
> 每次上传后需要重新生成缓存。

#### 图片标注

1. 进入“标注”页面
2. 选择图片和“生成描述”
3. 确认候选描述后提交重命名



### 资源包功能

本次重构的生产入口已移除本地/在线资源包导入、启停、社区同步和 ZIP 导出。资源包功能后续单独设计和实现；当前图片直接存放在 `.env` 配置的图片根目录中。

<a id="-api"></a>
## 🔌 API

本项目开放 API 接口，规范检索入口为 `POST /search`：

### Endpoint
`POST http://localhost:8275/search`

### 请求参数
| 参数名 | 类型 | 简介 | 是否必填 | 范围 |
|-----------|--------|-----------------------------------------------|----------|----------------------|
| `query`       | string | 要查询的内容（例如关键词或某个话题）  | ✅       | 非空 |
| `n_results`       | integer| 返回的图片数量 | 否 | 1 - 30 |
| `llm_enhance`       | boolean| 是否启用 LLM 查询增强 | 否 | 默认 false |

### 返回格式
返回格式为 JSON：

```json
{"results":["/media/example.png"]}
```

缓存生成和批量标注是长任务，接口立即返回 `202` 和 `task_id`，使用 `GET /tasks/{task_id}` 轮询。


<a id="-related-applications"></a>
## 📦 Related Applications

Mememeow 相关应用:

| 应用 | 作者   | GitHub | 链接 |
| --- | --- | --- | --- |
| VVQuest网页端 |  | [VVQuest](https://github.com/DanielZhangyc/VVQuest) | [链接](https://zvv.quest) |
| VVQuest*iOS*捷径 | [TomSmith163](https://github.com/TomSmith163) |  | [链接](https://www.icloud.com/shortcuts/a7084c7ae29e4de5898ce7c8386705f3) |
| HakuBot().vv() 命令 | [apple_catwaii](https://github.com/Apple-QAQ) |  | [QQ](https://qm.qq.com/cgi-bin/qm/qr?k=GJSCe1_B98V4Ni6leVtKAjQrAtJW-VG5 ) |
| VVQuest油猴脚本 | [DanielZhangyc](https://github.com/DanielZhangyc) | [vvquest-tampermonkey-extension](https://github.com/DanielZhangyc/vvquest-tampermonkey-extension) | [greasyfork](https://greasyfork.org/zh-CN/scripts/528477-vvquest-vv%E8%A1%A8%E6%83%85%E5%8C%85%E5%8A%A9%E6%89%8B) |
| Yunzai-Bot 插件 | [TomyJan](https://github.com/TomyJan) | [TomyJan/Yunzai-TomyJan-Plugin](https://github.com/TomyJan/Yunzai-TomyJan-Plugin/) |  |


> [!TIP]
> 如果你想添加你的应用，请提交 [PR](https://github.com/MemeMeow-Studio/MemeMeow/pulls) 或 [Issue](https://github.com/MemeMeow-Studio/MemeMeow/issues)

## 📄 License

本项目采用 [MIT](LICENSE) 开源协议。

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=MemeMeow-Studio/MemeMeow&type=Date)](https://star-history.com/#MemeMeow-Studio/MemeMeow&Date)

---

## 旧版本归档

迁移前的 Streamlit 实现已移动到 [`legacy/streamlit-v1/`](legacy/streamlit-v1/)，仅供查阅，不参与构建、测试或部署。完整的 Git 基线由 `streamlit-v1` 标签保留。
