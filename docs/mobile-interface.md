# 手机界面

## 导航和页面空间

宽度不超过 760 CSS px 时，`WorkspaceNav` 在页面底部展示检索、图片库、上传、任务和更多。更多页面从传入的 `pages` 中读取，文档地址由 `docsUrl` 提供。父组件继续处理 `navigate` 事件，使用路由的适配层继续保留原有 URL 和权限检查。

`ServiceStatus` 接收 `config`、`embeddingState` 和 `embeddingStateLabel`。桌面顶栏与手机页面标题区域读取同一份状态。

`mobile.css` 通过 `--mobile-header-height`、`--mobile-nav-height`、`--safe-area-top` 和 `--safe-area-bottom` 统一处理顶栏、导航和页面留白。手机底部导航高度为 64px，加上设备底部安全区域；顶栏高度为 64px，加上顶部安全区域。

## 图片库操作

手机图片库常驻文件名筛选、排序、刷新、选择图片和管理入口。选择图片后展示批量操作；结束选择时清理当前目标。方块和列表两种展示方式保留同一份选择状态。

`ActionMenu` 的操作在桌面直接展示，在手机通过展开按钮展示。Floating UI 根据视口调整浮层位置。Escape 恢复展开按钮的焦点；模态框打开期间保留原操作入口，供对话框关闭后恢复焦点。

列表保留三个阶段的状态文字，详情继续展示逐阶段状态和具体错误原因。维护操作保持原有确认与提交流程。

## 弹窗与输入

检索详情在手机使用单列，图片保持比例，匹配度位于图片下方。弹窗按内容确定高度，以动态视口和安全区域限制最大高度。关闭入口与主要操作保持可见，内容独立滚动。

处理选项与选中重试的字段位于 `processing-options-fields`，确认操作位于独立的 `processing-options-actions`。`useModalDialog` 在模态框存在期间限制背景滚动，关闭全部模态框后恢复此前的滚动设置。

手机导航与帮助按钮的点击区域至少为 44 × 44px，输入文字至少为 16px。viewport 包含 `viewport-fit=cover` 和 `interactive-widget=resizes-content`。

## 真实页面验证

使用工作区的已有开发入口启动服务，执行 `node frontend/scripts/validate-mobile-ui.mjs`。通过 `MOBILE_UI_BASE_URL` 指定已有前端地址，通过 `MOBILE_UI_OUTPUT_DIR` 指定忽略目录中的结果位置。

服务器适配层使用 `MOBILE_UI_MODE=server`，并通过 `MOBILE_UI_CREDENTIALS` 指定独立测试账户的 JSON 凭据文件。脚本读取凭据完成真实登录，不输出密码。`MOBILE_UI_UPLOAD_FIXTURES=true` 会将两个真实页面截图作为带测试标识的图片上传；使用此选项前必须确认目标属于开发环境。

脚本检查 320、375、390、430、600、768、1280 CSS px 的页面，记录顶部和底部截图、横向尺寸、导航点击区域、帮助按钮尺寸、输入字号、图片行位置、操作菜单和短视口弹窗。检查使用真实接口，不替换 API 响应。

`MOBILE_UI_RETRY_FIXTURE=true` 会选择名称带 `mobile-ui-` 标识的验收图片，通过页面重新提交视觉向量生成任务，检查批量入口隐藏后的焦点恢复，并查询任务至 `succeeded`。脚本同时检查操作菜单的 Enter、Tab、Escape 行为，以及短视口滚动后最后一个处理选项的完整可见性。

Chromium 触摸模拟不能证明设备安全区域、系统键盘、Safari、Android WebView、相册选择和图片保存的实际表现；这些项目需要真实设备提供独立证据。
