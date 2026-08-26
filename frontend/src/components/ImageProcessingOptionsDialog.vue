<script setup lang="ts">
/** 图片处理选项对话框：只负责安全默认值、能力提示和键盘焦点闭环。 */
import { shallowRef, watch } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { ImageProcessingOptions } from '../types'

const props = defineProps<{
  reverseImageAvailable: boolean
  reverseImageReason?: string
  busy: boolean
  returnFocus?: HTMLElement | null
  initialOptions?: ImageProcessingOptions
}>()

const emit = defineEmits<{
  confirm: [options: ImageProcessingOptions]
  cancel: []
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const firstOption = shallowRef<HTMLInputElement | null>(null)

/** 将传入的历史或失败重试选项收束为当前能力下的安全状态。 */
function normalizeOptions(options?: ImageProcessingOptions): ImageProcessingOptions {
  return {
    reverse_image_policy: props.reverseImageAvailable && options?.reverse_image_policy === 'auto' ? 'auto' : 'forbid',
    auto_name: options?.auto_name === true,
  }
}

const initialState = normalizeOptions(props.initialOptions)
const reversePolicy = shallowRef<ImageProcessingOptions['reverse_image_policy']>(initialState.reverse_image_policy)
const autoName = shallowRef(initialState.auto_name)

watch(() => props.initialOptions, (options) => {
  const next = normalizeOptions(options)
  reversePolicy.value = next.reverse_image_policy
  autoName.value = next.auto_name
})

watch(() => props.reverseImageAvailable, (available) => {
  // 能力在对话框打开期间失效时，不能把已经禁用的联网选择继续提交。
  if (!available && reversePolicy.value === 'auto') reversePolicy.value = 'forbid'
})

useModalDialog({
  dialog,
  initialFocus: firstOption,
  returnFocus: props.returnFocus,
  close: () => { if (!props.busy) emit('cancel') },
})

/** 提交本次对话框选择，busy 期间忽略重复确认。 */
function confirm(): void {
  if (props.busy) return
  emit('confirm', { reverse_image_policy: reversePolicy.value, auto_name: autoName.value })
}

/** 关闭对话框；提交中的状态必须忽略鼠标、键盘和测试触发的重复关闭。 */
function cancel(): void {
  if (!props.busy) emit('cancel')
}
</script>

<template>
  <div class="image-dialog-backdrop" role="presentation" @click.self="cancel">
    <section ref="dialog" class="image-dialog compact-dialog processing-options-dialog" role="dialog" aria-modal="true" aria-labelledby="processing-options-title" tabindex="-1">
      <header class="image-dialog-head">
        <div>
          <h2 id="processing-options-title">图片处理选项</h2>
        </div>
        <button class="quiet" type="button" :disabled="busy" aria-label="取消图片处理" @click="cancel">取消</button>
      </header>
      <form class="processing-options-form" @submit.prevent="confirm">
        <fieldset class="option-fieldset">
          <legend>反向图片检索</legend>
          <label class="option-choice">
            <input ref="firstOption" v-model="reversePolicy" type="radio" value="forbid" :disabled="busy" />
            <span><strong>禁止联网</strong><small>仅使用本地处理能力</small></span>
          </label>
          <label class="option-choice" :class="{ disabled: !reverseImageAvailable }">
            <input v-model="reversePolicy" type="radio" value="auto" :disabled="busy || !reverseImageAvailable" />
            <span><strong>按需允许联网</strong><small>{{ reverseImageAvailable ? 'Agent 需要时才会使用' : reverseImageReason || '服务不可用' }}</small></span>
          </label>
        </fieldset>
        <label class="option-toggle">
          <input v-model="autoName" type="checkbox" :disabled="busy" />
          <span><strong>按标题自动命名</strong><small>处理完成后尝试更新文件名</small></span>
        </label>
        <div class="processing-options-actions">
          <button class="quiet" type="button" :disabled="busy" @click="cancel">取消</button>
          <button class="primary" type="submit" :disabled="busy">{{ busy ? '提交中...' : '确认并提交' }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
