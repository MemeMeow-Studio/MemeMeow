<script setup lang="ts">
/** 图片库方块展示：保留稳定 ID 选择，展示三项真实状态，并把图片操作交回工作区。 */
import { computed, onBeforeUnmount, onMounted, shallowRef, useId, useTemplateRef, watch } from 'vue'
import { autoUpdate, flip, offset, shift, useFloating } from '@floating-ui/vue'
import type { MemeImage } from '../../types'
import { embeddingLabel, imageDisplayUrl, imageKey, metadataLabel, visualEmbeddingLabel } from '../../utils/presentation'
import ResilientImage from '../ResilientImage.vue'

const props = withDefaults(defineProps<{
  images: MemeImage[]
  selectedIds: ReadonlySet<string>
  disabled: boolean
  deletingImageId?: string | null
  displayName?: (item: MemeImage) => string
  savedFilename?: (item: MemeImage) => string
}>(), {
  deletingImageId: null,
  displayName: (item: MemeImage) => item.filename,
  savedFilename: (item: MemeImage) => item.filename,
})

const emit = defineEmits<{
  'toggle-selection': [item: MemeImage]
  preview: [item: MemeImage, trigger: HTMLElement]
  rename: [item: MemeImage]
  delete: [item: MemeImage]
}>()

const activeId = shallowRef<string | null>(null)
const reference = shallowRef<HTMLElement | null>(null)
const popover = useTemplateRef<HTMLElement>('actionsPopover')
const popoverId = useId()
const activeImage = computed(() => props.images.find(item => imageKey(item) === activeId.value))
const open = computed(() => !!activeImage.value)
const { floatingStyles, isPositioned } = useFloating(reference, popover, {
  open,
  placement: 'right-start',
  strategy: 'fixed',
  middleware: [offset(8), flip({ fallbackAxisSideDirection: 'end' }), shift({ padding: 8, crossAxis: true })],
  whileElementsMounted: autoUpdate,
})

/** 关闭浮窗；Escape 和主动关闭时将焦点归还图片上的操作按钮。 */
function closeMenu(restoreFocus = true): void {
  if (restoreFocus) reference.value?.focus()
  activeId.value = null
  reference.value = null
}

/** 从图片右上角按钮打开相邻浮窗，并让键盘可立即访问其中的操作。 */
function toggleMenu(item: MemeImage, event: MouseEvent): void {
  if (activeId.value === imageKey(item)) {
    closeMenu()
    return
  }
  reference.value = event.currentTarget as HTMLElement
  activeId.value = imageKey(item)
}

/** 点击缩略图时传回仍保留在页面上的按钮，供详情关闭后恢复焦点。 */
function preview(item: MemeImage, event: MouseEvent): void {
  closeMenu(false)
  emit('preview', item, event.currentTarget as HTMLElement)
}

/** 把浮窗操作发送给工作区，详情使用图片上的原始按钮作为焦点返回位置。 */
function performAction(action: 'preview' | 'rename' | 'delete'): void {
  const item = activeImage.value
  const trigger = reference.value
  if (!item || !trigger) return
  closeMenu()
  if (action === 'preview') emit('preview', item, trigger)
  else if (action === 'rename') emit('rename', item)
  else emit('delete', item)
}

/** 点击或键盘焦点移出浮窗和对应按钮时关闭浮窗，保留用户的新焦点位置。 */
function dismissOutside(event: Event): void {
  if (!open.value) return
  const target = event.target as Node
  if (!popover.value?.contains(target) && !reference.value?.contains(target)) closeMenu(false)
}

/** Escape 只关闭当前图片操作浮窗，后续图片详情沿用自己的键盘处理。 */
function handleEscape(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || !open.value) return
  event.preventDefault()
  event.stopPropagation()
  closeMenu()
}

watch([isPositioned, activeId], ([positioned, id]) => {
  if (positioned && id) popover.value?.querySelector<HTMLButtonElement>('button')?.focus()
}, { flush: 'post' })
watch(activeImage, item => { if (!item) closeMenu(false) })
watch(() => props.disabled, disabled => { if (disabled) closeMenu(false) })
onMounted(() => {
  document.addEventListener('pointerdown', dismissOutside)
  document.addEventListener('focusin', dismissOutside)
  document.addEventListener('keydown', handleEscape)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', dismissOutside)
  document.removeEventListener('focusin', dismissOutside)
  document.removeEventListener('keydown', handleEscape)
})
</script>

<template>
  <div class="library-image-grid" role="list" aria-label="图片库方块展示">
    <article v-for="item in props.images" :key="imageKey(item)" class="library-image-tile" role="listitem">
      <div class="library-tile-preview-row">
        <div class="library-tile-square" :class="{ selected: props.selectedIds.has(imageKey(item)) }">
          <button class="library-tile-preview" type="button" :aria-label="`查看 ${props.savedFilename(item)} 图片与详情`" :disabled="props.disabled" @click="preview(item, $event)">
            <ResilientImage :src="imageDisplayUrl(item)" :fallback-src="item.media_url" :alt="`预览 ${props.savedFilename(item)}`" />
          </button>
          <label class="library-tile-check">
            <input type="checkbox" :checked="props.selectedIds.has(imageKey(item))" :disabled="props.disabled" :aria-label="`选择 ${props.savedFilename(item)}`" @change="emit('toggle-selection', item)" />
          </label>
          <button
            class="quiet library-tile-menu-trigger"
            type="button"
            :aria-label="`${props.savedFilename(item)}的操作`"
            :aria-expanded="activeId === imageKey(item)"
            :aria-controls="activeId === imageKey(item) ? popoverId : undefined"
            aria-haspopup="dialog"
            :disabled="props.disabled"
            @click="toggleMenu(item, $event)"
          >⋯</button>
        </div>
        <div class="library-tile-status" aria-label="图片状态摘要">
          <span class="library-status-bar" :data-state="item.metadata?.status || 'unknown'" data-stage="context" role="img" :title="`语境：${metadataLabel(item.metadata?.status)}`" :aria-label="`语境：${metadataLabel(item.metadata?.status)}`"></span>
          <span class="library-status-bar" :data-state="item.embedding_status || 'unknown'" data-stage="text" role="img" :title="embeddingLabel(item.embedding_status)" :aria-label="embeddingLabel(item.embedding_status)"></span>
          <span class="library-status-bar" :data-state="item.visual_embedding_status || 'unknown'" data-stage="visual" role="img" :title="visualEmbeddingLabel(item.visual_embedding_status)" :aria-label="visualEmbeddingLabel(item.visual_embedding_status)"></span>
        </div>
      </div>
      <strong class="library-tile-name" :title="props.displayName(item)">{{ props.displayName(item) }}</strong>
    </article>
    <div v-if="!props.images.length" class="empty-state compact"><h2>图片库还没有图片</h2></div>
  </div>
  <Teleport to="body">
    <div
      v-if="activeImage"
      :id="popoverId"
      ref="actionsPopover"
      class="library-image-popover"
      :style="[floatingStyles, { visibility: isPositioned ? 'visible' : 'hidden' }]"
      role="dialog"
      :aria-label="`${props.displayName(activeImage)}的操作`"
    >
      <button class="quiet" type="button" :disabled="props.disabled" @click="performAction('preview')">查看详情</button>
      <button class="quiet" type="button" :disabled="props.disabled" @click="performAction('rename')">重命名</button>
      <button class="quiet library-tile-delete" type="button" :disabled="props.disabled" :aria-label="`删除图片 ${props.savedFilename(activeImage)}`" @click="performAction('delete')">{{ props.deletingImageId === activeImage.meme_id ? '删除中...' : '删除' }}</button>
    </div>
  </Teleport>
</template>

<style scoped>
.library-image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 20px 16px; }
.library-image-tile { min-width: 0; }
.library-tile-preview-row { display: flex; gap: 7px; }
.library-tile-square { position: relative; flex: 1; min-width: 0; aspect-ratio: 1; border: 1px solid var(--line); border-radius: 4px; background: var(--surface-muted); }
.library-tile-square.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.library-tile-preview { position: absolute; inset: 0; width: 100%; height: 100%; min-height: 0; padding: 6px; border: 0; background: transparent; }
.library-tile-preview:deep(.resilient-image) { width: 100%; height: 100%; }
.library-tile-preview:deep(img) { display: block; width: 100%; height: 100%; object-fit: contain; }
.library-tile-check { position: absolute; top: 6px; left: 6px; display: grid; place-items: center; width: 28px; height: 28px; border-radius: 4px; background: var(--surface); cursor: pointer; }
.library-tile-check input { width: 16px; height: 16px; min-height: 0; margin: 0; padding: 0; accent-color: var(--accent); cursor: inherit; }
.library-tile-menu-trigger { position: absolute; top: 6px; right: 6px; width: 28px; height: 28px; min-height: 0; padding: 0; border-radius: 4px; }
.library-tile-status { display: flex; flex-direction: column; justify-content: center; gap: 6px; width: 5px; padding: 12px 0; }
.library-status-bar { flex: 1; border-radius: 3px; background: var(--line-strong); }
.library-status-bar[data-state="ready"] { background: var(--success); }
.library-status-bar[data-state="partial"], .library-status-bar[data-state="waiting_for_text"] { background: var(--warning); }
.library-status-bar[data-state="repair_required"], .library-status-bar[data-state="blocked"] { background: var(--error); }
.library-tile-name { display: block; overflow: hidden; margin-top: 7px; color: var(--ink-soft); font-size: 14px; white-space: nowrap; text-overflow: ellipsis; }
.library-image-grid > .empty-state { grid-column: 1 / -1; }
.library-image-popover { display: flex; gap: 7px; z-index: 100; max-width: calc(100vw - 16px); padding: 10px; border: 1px solid var(--line-strong); border-radius: var(--radius-surface); background: var(--surface); box-shadow: var(--shadow-drawer); }
.library-image-popover button { min-height: 44px; padding: 7px 12px; white-space: nowrap; }
.library-tile-delete { color: var(--error); }
@media (pointer: coarse) {
  .library-tile-check, .library-tile-menu-trigger { width: 44px; height: 44px; }
}
</style>
