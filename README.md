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

MemeMeow 是一个基于自然语言的表情包检索工具。它能让你通过描述想要的场景或心情，快速找到合适的表情包（无需记住具体的文件名或标签）。

> [!CAUTION]
> 本项目返回的表情包结果均由 AI 理解和生成，与开发者本人观点无关。

<a id="-features"></a>

## ✨ 核心特性

- **🤖 自然语言检索**: 采用 Embedding 模型，实现 Q&A 式的检索，能够直接根据你的描述（如“表达无奈”）找到最匹配的图片。
- **🧠 自动化语境理解**: 内置 OpenCode Agent，当你上传图片时，Agent 会自动对图片进行结构化分析、生成语境和描述。
- **👁️ 视觉近邻搜索**: 支持本地 DINOv2 视觉模型，实现图片之间的高效相似度搜索。
- **📦 开箱即用**: 提供现代化的 Vue 3 Web 界面、统一的 API，所有服务通过 Docker Compose 一键拉起。
- **⚡ 高并发调度**: 内置受控并发队列，支持长任务异步处理（如批量图片导入、后台模型推理），确保单机部署下依然稳定。

<a id="-screenshots"></a>

## 📸 项目截图

当前界面已全面升级为 Vue 3 提供支持的现代化 UI。
*(界面截图正在更新中，可直接访问 [在线体验](https://zvv.quest) 查看最新效果)*

## ℹ️ 数据声明

本项目内置的初始表情包数据来源于 [知乎](https://www.zhihu.com/question/656505859/answer/55843704436)。若有侵权，请联系删除。

<a id="-quick-start"></a>

## 🚀 快速开始

本项目依赖容器化环境进行隔离与自动化部署，请确保机器已安装好最新版的 **Docker Engine** 与 **Compose v2 插件**。

### 1. 克隆代码与配置环境变量

```bash
git clone https://github.com/MemeMeow-Studio/MemeMeow.git
cd MemeMeow

# 如果你没有安装 uv，也可以直接复制 .env.example 并手动编辑:
# cp .env.example .env
uv run python -m scripts.sync_env
```

执行完毕后，请编辑项目目录下的 `.env` 文件，填写必要的模型配置（特别是 `MEMEMEOW_OPENCODE_BASE_URL` 与 `MEMEMEOW_OPENCODE_API_KEY`，用于 Agent 处理图片语境）。

### 2. 一键启动

```bash
./start.sh start
```

该命令会自动拉起 PostgreSQL（附带 pgvector）、代理执行器 (Agent Executor)、视觉服务以及后端 API 容器。
服务启动成功后，浏览器访问 `http://127.0.0.1:8275/` 即可看到前端界面！

### 3. 可选：下载本地视觉权重

如果你想要启用“视觉相似度”功能，需要下载官方 DINOv2 权重：

```bash
mkdir -p data/models
curl -L --fail --output data/models/dinov2_vitb14_pretrain.pth \
  https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/dinov2_vitb14_pretrain.pth
```

下载完成后，若环境配置无误，容器内部会自动挂载并启动服务。

### 常用运维命令

```bash
./start.sh status         # 查看运行状态
./start.sh logs           # 查看所有服务的日志
./start.sh logs mememeow -f  # 持续跟踪主后端的日志
./start.sh stop           # 安全停止服务（不会丢失数据）
```

<a id="-usage"></a>

## 📖 使用说明

1. **搜索图片**：在主界面的搜索框输入你想要的表情包场景描述，系统会自动检索匹配。
2. **上传与自动化处理**：
   - 进入“上传”页面选择文件。图片上传后会进入后台任务队列。
   - 系统将自动执行：生成视觉向量 → Agent 分析语境 → 可选重命名 → 文本 Embedding。
   - 所有任务的进度可以在“处理任务”页面实时查看。如果某个阶段失败，可以在侧边栏点击重新尝试。
3. **资源合集包**：系统支持导入 `.zip` 格式的表情包资源包（`mememeow-collection`），支持快速分享与批量入库。

<a id="-api"></a>

## 🔌 进阶设计与 API

MemeMeow 本质是一个 API 驱动的服务。如果你是开发者，可以直接调用 `POST /search` 进行跨应用整合，只要不涉及后台管理，所有检索 API 皆可调用。

* **Endpoint**: `POST http://localhost:8275/search`
* **JSON 请求示例**: `{"query": "无奈叹气", "n_results": 5, "llm_enhance": false}`
* **JSON 响应示例**: `{"results":["/media/uuid-1", "/media/uuid-2"]}`

**🛠️ 面向开发者的架构文档**

如果你想了解 MemeMeow 更底层的设计机制（如安全边界、权限管理、前后端联调方案），我们将其抽离到了独立的文档中：
👉 **[高级部署与架构指南 (docs/deployment.md)](docs/deployment.md)**

关于双环境隔离设计与作用域约束，请查阅 [应用作用域设计 (docs/application-scope.md)](docs/application-scope.md)。

<a id="-related-applications"></a>

## 📦 社区相关应用

基于 MemeMeow 衍生出的优秀社区项目：

| 应用                | 作者                                             | GitHub                                                                                           | 链接                                                                                                             |
| ------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| VVQuest网页端       |                                                  | [VVQuest](https://github.com/DanielZhangyc/VVQuest)                                               | [链接](https://zvv.quest)                                                                                         |
| VVQuest*iOS*捷径  | [TomSmith163](https://github.com/TomSmith163)     |                                                                                                  | [链接](https://www.icloud.com/shortcuts/a7084c7ae29e4de5898ce7c8386705f3)                                         |
| HakuBot().vv() 命令 | [apple_catwaii](https://github.com/Apple-QAQ)     |                                                                                                  | [QQ](https://qm.qq.com/cgi-bin/qm/qr?k=GJSCe1_B98V4Ni6leVtKAjQrAtJW-VG5)                                          |
| VVQuest油猴脚本     | [DanielZhangyc](https://github.com/DanielZhangyc) | [vvquest-tampermonkey-extension](https://github.com/DanielZhangyc/vvquest-tampermonkey-extension) | [greasyfork](https://greasyfork.org/zh-CN/scripts/528477-vvquest-vv%E8%A1%A8%E6%83%85%E5%8C%85%E5%8A%A9%E6%89%8B) |
| Yunzai-Bot 插件     | [TomyJan](https://github.com/TomyJan)             | [TomyJan/Yunzai-TomyJan-Plugin](https://github.com/TomyJan/Yunzai-TomyJan-Plugin/)                |                                                                                                                  |

> [!TIP]
> 如果你想将自己开发的衍生应用加到这个列表，欢迎提交 [PR](https://github.com/MemeMeow-Studio/MemeMeow/pulls) 或 [Issue](https://github.com/MemeMeow-Studio/MemeMeow/issues)！

## 📄 License & ⭐ Star History

本项目采用 [MIT](LICENSE) 开源协议。

[![Star History Chart](https://api.star-history.com/svg?repos=MemeMeow-Studio/MemeMeow&type=Date)](https://star-history.com/#MemeMeow-Studio/MemeMeow&Date)

*(注：迁移前的 Streamlit 旧版本已归档至 `legacy/streamlit-v1/`，不再参与当前版本的构建与测试)*
