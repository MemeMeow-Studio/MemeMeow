<script setup lang="ts">
/** 公共操作分组：桌面直接展示操作，手机通过明确的按钮展开操作列表。 */
import { onBeforeUnmount, shallowRef, useId, watch } from 'vue'
import { autoUpdate, flip, offset, shift, useFloating } from '@floating-ui/vue'

defineProps<{ label: string; disabled?: boolean }>()

const expanded = shallowRef(false)
const trigger = shallowRef<HTMLButtonElement | null>(null)
const panel = shallowRef<HTMLElement | null>(null)
const panelId = useId()
const { x, y, isPositioned } = useFloating(trigger, panel, {
  open: expanded,
  strategy: 'fixed',
  placement: 'bottom-end',
  middleware: [offset(6), flip({ padding: 12 }), shift({ padding: 12 })],
  whileElementsMounted: autoUpdate,
})

/** 关闭当前操作列表，并将键盘焦点恢复到对应的展开按钮。 */
function close(): void {
  expanded.value = false
  trigger.value?.focus()
}

/** 点击操作分组之外时关闭列表；已打开的模态框保留其返回焦点。 */
function dismissOutside(event: PointerEvent): void {
  if (document.querySelector('[aria-modal="true"]')) return
  const target = event.target as Node
  if (trigger.value?.contains(target) || panel.value?.contains(target)) return
  expanded.value = false
}

watch(expanded, (open) => {
  if (open) document.addEventListener('pointerdown', dismissOutside)
  else document.removeEventListener('pointerdown', dismissOutside)
})

/** 浮层完成定位后聚焦首个可用操作，使键盘可以直接访问展开的列表。 */
watch([expanded, isPositioned], ([open, positioned]) => {
  if (open && positioned) panel.value?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus()
}, { flush: "post" })
onBeforeUnmount(() => document.removeEventListener('pointerdown', dismissOutside))
</script>

<template>
  <div class="action-menu" @keydown.esc.prevent.stop="close">
    <button
      ref="trigger"
      class="quiet action-menu-toggle"
      type="button"
      :disabled="disabled"
      :aria-expanded="expanded"
      :aria-controls="panelId"
      @click="expanded = !expanded"
    >{{ label }}</button>
    <div ref="panel" :id="panelId" class="action-menu-items" :class="{ expanded, positioned: isPositioned }" :style="{ '--action-menu-x': `${x}px`, '--action-menu-y': `${y}px` }" role="group" :aria-label="label">
      <slot />
    </div>
  </div>
</template>
