<script setup lang="ts">
/** 全应用 toast 容器；进入模态框时移动到其内部，保持键盘和读屏可访问。 */
import { Teleport } from 'vue'
import { Toaster } from 'vue-sonner'
import 'vue-sonner/style.css'
import { toastTarget } from '../composables/useToast'
</script>

<template>
  <Teleport :to="toastTarget">
    <Toaster
      class="app-toasts"
      position="bottom-right"
      expand
      close-button
      :visible-toasts="Infinity"
      :duration="3000"
      :offset="24"
      :mobile-offset="80"
      container-aria-label="操作提示，按 Alt+T 进入"
      :toast-options="{ closeButtonAriaLabel: '关闭提示' }"
    />
  </Teleport>
</template>

<style>
.app-toasts[data-sonner-toaster] { z-index: 2000; display: flex; flex-direction: column; gap: 12px; width: min(380px, calc(100vw - 24px)); height: auto; max-height: calc(100dvh - 112px - env(safe-area-inset-top) - env(safe-area-inset-bottom)); overflow-y: auto; padding: 4px; }
.app-toasts [data-sonner-toast][data-styled=true] { position: relative; top: auto; width: 100%; flex: 0 0 auto; transform: none; padding: 16px 58px 16px 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink); font-size: 14px; line-height: 1.5; font-family: inherit; box-shadow: 0 4px 16px #2a26381a; }
.app-toasts [data-title] { overflow-wrap: anywhere; white-space: pre-wrap; }
.app-toasts [data-sonner-toast][data-type=success] { border-color: var(--line-success); color: var(--success); background: var(--surface-success); }
.app-toasts [data-sonner-toast][data-type=error] { border-color: var(--line-error); color: var(--error); background: var(--surface-error); }
.app-toasts [data-sonner-toast][data-type=warning] { border-color: var(--line-warning); color: var(--warning); background: var(--surface-warning); }
.app-toasts [data-sonner-toast] [data-close-button] { top: 4px; right: 4px; left: auto; width: 44px; height: 44px; transform: none; border: 0; background: transparent; color: inherit; }
.app-toasts [data-close-button]:focus-visible { outline: 2px solid var(--focus); outline-offset: -4px; }
@media (max-width: 760px) { .app-toasts[data-sonner-toaster] { right: 12px; left: 12px; margin: 0 auto; max-height: calc(100dvh - 160px - env(safe-area-inset-top) - env(safe-area-inset-bottom)); } }
@media (prefers-reduced-motion: reduce) { .app-toasts [data-sonner-toast] { animation: none; transition: none; } }
</style>
