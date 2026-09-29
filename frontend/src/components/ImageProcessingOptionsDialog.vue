<script setup lang="ts">
/** 图片处理选项对话框：只负责安全默认值、能力提示和键盘焦点闭环。 */
import { computed, shallowRef, watch } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { ImageProcessingOptions } from '../types'

const props = defineProps<{
  reverseImageAvailable: boolean
  reverseImageReason?: string
  busy: boolean
  returnFocus?: HTMLElement | null
  initialOptions?: ImageProcessingOptions
  processingTarget?: 'full_retry' | 'repair'
  upload?: boolean
}>()

const emit = defineEmits<{
  confirm: [options: ImageProcessingOptions]
  cancel: []
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const firstOption = shallowRef<HTMLInputElement | null>(null)
const uploadOption = shallowRef<HTMLInputElement | null>(null)
const initialFocus = computed(() => props.upload ? uploadOption.value : firstOption.value)

/** 将传入的历史或失败重试选项收束为当前能力下的安全状态。 */
function normalizeOptions(options?: ImageProcessingOptions): ImageProcessingOptions {
  return {
    reverse_image_policy: props.reverseImageAvailable && options?.reverse_image_policy === 'auto' ? 'auto' : 'forbid',
    auto_name: options?.auto_name === true,
    upload_mode: options?.upload_mode === 'direct' ? 'direct' : 'analyze',
  }
}

const initialState = normalizeOptions(props.initialOptions)
const reversePolicy = shallowRef<ImageProcessingOptions['reverse_image_policy']>(initialState.reverse_image_policy)
const autoName = shallowRef(initialState.auto_name)
const uploadMode = shallowRef(initialState.upload_mode)

watch(() => props.initialOptions, (options) => {
  const next = normalizeOptions(options)
  reversePolicy.value = next.reverse_image_policy
  autoName.value = next.auto_name
  uploadMode.value = next.upload_mode
})

watch(() => props.reverseImageAvailable, (available) => {
  // 能力在对话框打开期间失效时，不能把已经禁用的联网选择继续提交。
  if (!available && reversePolicy.value === 'auto') reversePolicy.value = 'forbid'
})

useModalDialog({
  dialog,
  initialFocus,
  returnFocus: props.returnFocus,
  close: () => { if (!props.busy) emit('cancel') },
})

/** 提交本次对话框选择，busy 期间忽略重复确认。 */
function confirm(): void {
  if (props.busy) return
  emit('confirm', {
    reverse_image_policy: reversePolicy.value,
    auto_name: autoName.value,
    ...(props.upload ? { upload_mode: uploadMode.value } : {}),
  })
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
          <p class="processing-options-description">
            {{ props.upload ? '选择上传后的处理方式。超过 10 张默认直接上传。' : props.processingTarget === 'repair' ? '只为存在问题的图片和阶段创建修复任务。' : '所有已启用阶段都会重新执行，即使已有结果仍然有效。' }}
          </p>
        </div>
        <button class="quiet" type="button" :disabled="busy" aria-label="取消图片处理" @click="cancel">取消</button>
      </header>
      <form class="processing-options-form" @submit.prevent="confirm">
        <fieldset v-if="props.upload" class="option-fieldset">
          <legend>上传方式</legend>
          <label class="option-choice">
            <input ref="uploadOption" v-model="uploadMode" type="radio" value="direct" :disabled="busy" />
            <span><strong>直接上传</strong><small>生成图片与有效文本向量，后台自动命名。</small></span>
          </label>
          <label class="option-choice">
            <input v-model="uploadMode" type="radio" value="analyze" :disabled="busy" />
            <span><strong>完整分析</strong><small>分析图片语境并生成检索向量。</small></span>
          </label>
        </fieldset>
        <fieldset v-if="!props.upload || uploadMode === 'analyze'" class="option-fieldset">
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
        <label v-if="!props.upload || uploadMode === 'analyze'" class="option-toggle">
          <input v-model="autoName" type="checkbox" :disabled="busy" />
          <span><strong>按标题自动命名</strong><small>完整分析后使用语境标题更新名称。</small></span>
        </label>
        <div class="processing-options-actions">
          <button class="quiet" type="button" :disabled="busy" @click="cancel">取消</button>
          <button class="primary" type="submit" :disabled="busy">{{ busy ? '提交中...' : '确认并提交' }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
