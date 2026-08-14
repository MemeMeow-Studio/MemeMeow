---
name: MemeMeow
description: 简洁、可读且带一点趣味的本地表情包工作台，用自然语言检索、整理图片并追踪异步任务。
colors:
  ink: "#2A2638"
  ink-soft: "#39334D"
  workspace: "#F8F7FB"
  surface: "#FFFFFF"
  surface-muted: "#F0EEF8"
  divider: "#E3E0ED"
  divider-strong: "#C7C2D8"
  text-muted: "#625D72"
  text-muted-soft: "#716B82"
  accent: "#5157D9"
  accent-deep: "#3C42B1"
  success: "#1F7A5A"
  info: "#3B66A5"
  warning: "#8A651C"
  error: "#B64855"
  focus: "#3B43B8"
typography:
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.35
  control:
    fontSize: "16px"
    fontWeight: 500
    lineHeight: 1.2
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "36px"
    fontWeight: 750
    lineHeight: 1.15
rounded:
  control: "6px"
  surface: "8px"
spacing:
  compact: "12px"
  field: "16px"
  panel: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#FFFFFF"
    rounded: "{rounded.control}"
    minHeight: "46px"
  button-quiet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.control}"
    minHeight: "46px"
  heading-rule:
    use: "标题下使用短紫蓝斜线作为克制分隔，不作为大面积背景"
---

# Design System: MemeMeow

## Overview

**Creative North Star: “清晰的表达工作面”**

MemeMeow 是本地图片库的操作台。用户在浅色工作面上用自然语言查找、整理和修复表情包；信息先于装饰，真实图片是主要视觉对象。紫蓝只承担交互、当前导航和焦点，深墨承担品牌与正文，轻微倾斜的深色方块 M 是唯一克制品牌标记。

## Visual World

工作区以连续阅读轴组织：导航和标题说清楚“这里是什么”，紫蓝标识“现在可以做什么”。标题下的短斜线提供方向感，但不扩展成背景图案。按钮、输入和状态使用轻边框与稳定触达尺寸，图片内容保持不被装饰抢夺。

## Colors

- **Ink** (`#2A2638`) 与 **Ink soft** (`#39334D`)：品牌、标题、主要文本和正文。
- **Workspace** (`#F8F7FB`) 与 **Surface** (`#FFFFFF`)：工作区背景与图片/表单内容面。
- **Accent** (`#5157D9`)：主操作、当前导航和焦点，不用于大面积背景。
- **Divider** (`#E3E0ED`) 与 **Divider strong** (`#C7C2D8`)：列表、字段和分区边界。
- **Success** (`#1F7A5A`)、**Info** (`#3B66A5`)、**Warning** (`#8A651C`)、**Error** (`#B64855`)：状态必须带文字，不仅依赖颜色。
- **Focus** (`#3B43B8`)：键盘焦点环，保持与主交互色区分。

## Typography

全界面使用系统无衬线，优先稳定的中英文混排。正文基准为 16px/1.5，关键操作与输入为 16px，功能元数据为 14px，页面标题 36px（移动端 32px）。不使用 10-12px 装饰文字；通过重量、间距和轻边框区分状态。

## Layout

桌面保留 232px 侧栏与最大 1280px 内容面，标题、检索表单和列表沿同一条阅读轴排列。图片库与任务页使用分隔行而非卡片堆叠；设置页保留同一分隔节奏。移动端保留顶部五项路由导航，导航按钮最小高度 50px，正文左右留 18px；表格变为带字段标签的行，抽屉和图片 JSON 预览变为全屏。

## Controls and States

- 主按钮使用紫蓝和白色文字，禁用态改用中性灰并保持可读对比度。
- 安静按钮使用白底和强分隔线，hover/focus/disabled 维持同一尺寸，触达区不小于 44px。
- 输入、选择框和上传区域使用白色内容面；错误、成功、加载和空状态都同时提供文字说明。
- 任务状态通过中文标签、状态点和进度文字共同表达；不在用户界面显示后端枚举。
- 加载使用行骨架或明确的进行中文案；`prefers-reduced-motion` 下关闭过渡和骨架动画。移动端顶部继续保留“API 已连接”语义，但隐藏模型名称以节省垂直空间。

## Responsive Rules

移动端不压缩字体来塞入桌面结构：顶部保留“API 已连接”，五项导航保持稳定列宽；检索表单垂直堆叠，图片结果两列，工具栏和任务字段允许换行。320px 及以上宽度下，长文件名与 JSON 使用 `overflow-wrap: anywhere`，按钮组在需要时改为单列，禁止横向滚动和遮挡。

## Do's and Don'ts

### Do:

- **Do** 用文字状态、进度和恢复动作表达异步任务。
- **Do** 让标题下的短斜线提供分区方向，保持页面的一点性格。
- **Do** 在图片库和任务页保留分隔行、稳定列宽和可键盘操作的控件。

### Don't:

- **Don't** 把斜线铺满背景，或用它代替语义状态。
- **Don't** 用卡片套卡片、营销 hero、渐变和无目的动画抢夺工作区注意力。
- **Don't** 把原始任务枚举、低对比度灰字或只依赖颜色的状态交给用户阅读。

## 图片库与合集

图片库使用扁平分隔行展示当前 scope 的全部图片；合集使用相同的工作区导航和真实封面，成员关系不复制图片身份。移动端保持单列、可滚动的操作面。

## 后续需求

“最近看过”不在当前工作台展示，记录为后续独立需求。
