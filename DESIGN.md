---
name: MemeMeow
description: 用自然语言检索本地表情包的操作型工作台。
colors:
  ink: "#20242b"
  workspace: "#f5f6f8"
  surface: "#ffffff"
  divider: "#e5e7eb"
  text-muted: "#737b87"
  success: "#41a77a"
  error: "#c95b52"
typography:
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.4
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "28px"
    fontWeight: 600
    lineHeight: 1.2
rounded:
  control: "5px"
  surface: "7px"
spacing:
  compact: "8px"
  field: "12px"
  panel: "24px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "0 20px"
  button-quiet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-muted}"
    rounded: "{rounded.control}"
    padding: "0 12px"
---

# Design System: MemeMeow

## Overview

**Creative North Star: "The Focused Workbench"**

MemeMeow is a restrained local operations surface. The screen prioritizes repeated scanning: a stable sidebar, a generous but quiet work area, and dense task rows that expose state before detail. Color only communicates status or the currently selected action.

## Colors

Near-black anchors commands and identity; cool off-white workspace and white surfaces keep media and task status legible.

- **Ink** (`#20242b`): brand mark, primary command and selected navigation text.
- **Workspace** (`#f5f6f8`): continuous application background.
- **Success** (`#41a77a`) and **Error** (`#c95b52`): status only, always paired with text.

## Typography

The system UI stack carries all product text. Headings are fixed-size and compact; labels and operational metadata are smaller but remain readable.

## Layout

Desktop uses a 216px navigation rail and a centered content area capped at 1180px. Task lists are table-like at desktop widths and become labeled, separated rows below 760px. A task drawer is fixed to the viewport and becomes full-screen on mobile.

## Elevation & Depth

The interface is flat by default. Borders distinguish surfaces; only the task drawer uses a soft directional shadow to establish temporary focus.

## Shapes

Controls use 5px corners and framed tool surfaces use 7-8px corners. Sections are layouts, not floating cards. Repeated media and task rows use dividers rather than stacked cards.

## Components

### Buttons
- Primary actions use ink fill with white text.
- Quiet actions use a 1px divider border and a white surface.
- Disabled primary buttons reduce opacity without changing layout.

### Inputs / Fields
- Inputs and selects use white backgrounds, 1px neutral borders and 5px corners.
- Focus changes the border color; controls keep stable heights.

### Navigation
- Sidebar entries are text-first with one muted active background.
- Mobile keeps the same four destinations in a horizontal nav.

### Task List
- Status combines colored dot and text.
- Rows are clickable with a neutral selected state; detail appears in a fixed drawer.

## Do's and Don'ts

### Do:
- **Do** use status, progress, filters and row actions to express operations.
- **Do** preserve desktop table columns and mobile field labels for task scanning.
- **Do** keep loading visible with row skeletons rather than a blocking spinner.

### Don't:
- **Don't** add explanatory helper paragraphs below task controls or modules.
- **Don't** nest cards or turn application sections into decorative floating panels.
- **Don't** use status color without a textual state.
