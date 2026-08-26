<script setup lang="ts">
/** 图片库选中重试对话框：在完整流水线和三个独立核心阶段之间做单次选择。 */
import { computed, ref, shallowRef } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { CoreImageProcessingStage, SelectedImageRetryMode } from '../types'

const props = defineProps<{
  selectedCount: number
  busy: boolean
  returnFocus?: HTMLElement | null
}>()

const emit = defineEmits<{
  confirm: [payload: { mode: SelectedImageRetryMode; stages: CoreImageProcessingStage[] }]
  cancel: []
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const firstOption = shallowRef<HTMLInputElement | null>(null)
const mode = shallowRef<SelectedImageRetryMode>('full')
const stages = ref<CoreImageProcessingStage[]>([])

const stageOptions: Array<{ value: CoreImageProcessingStage; label: string; description: string }> = [
  { value: 'agent', label: '图片语境', description: '重新分析图片表达的内容和含义' },
  { value: 'text_embedding', label: '文本索引', description: '根据当前语境重新生成检索索引' },
  { value: 'visual', label: '图片向量', description: '重新生成图片的视觉向量' },
]

const confirmLabel = computed(() => mode.value === 'full' ? '完整重试' : `重试已选部分（${stages.value.length}）`)
const partsInvalid = computed(() => mode.value === 'parts' && stages.value.length === 0)

useModalDialog({
  dialog,
  initialFocus: firstOption,
  returnFocus: props.returnFocus,
  close: () => { if (!props.busy) emit('cancel') },
})

/** 提交当前重试范围；指定部分必须至少包含一个核心阶段。 */
function confirm(): void {
  if (props.busy || partsInvalid.value) return
  emit('confirm', { mode: mode.value, stages: [...stages.value] })
}

/** 关闭对话框；执行中的请求必须保持模态焦点和表单状态。 */
function cancel(): void {
  if (!props.busy) emit('cancel')
}
</script>

<template>
  <div class="image-dialog-backdrop" role="presentation" @click.self="cancel">
    <section
      ref="dialog"
      class="image-dialog compact-dialog processing-options-dialog retry-selected-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="retry-selected-title"
      tabindex="-1"
    >
      <header class="image-dialog-head">
        <div>
          <h2 id="retry-selected-title">重试选中</h2>
          <p>已选 {{ selectedCount }} 张图片</p>
        </div>
        <button class="quiet" type="button" :disabled="busy" aria-label="取消重试选中" @click="cancel">取消</button>
      </header>
      <form class="processing-options-form retry-selected-form" @submit.prevent="confirm">
        <fieldset class="option-fieldset">
          <legend>重试范围</legend>
          <label class="option-choice">
            <input ref="firstOption" v-model="mode" type="radio" value="full" :disabled="busy" />
            <span><strong>完整重试</strong><small>按原有完整流水线重新处理所选图片</small></span>
          </label>
          <label class="option-choice">
            <input v-model="mode" type="radio" value="parts" :disabled="busy" />
            <span><strong>指定部分</strong><small>只重新提交下面勾选的核心处理阶段</small></span>
          </label>
        </fieldset>

        <fieldset v-if="mode === 'parts'" class="option-fieldset retry-stage-fieldset" :aria-describedby="partsInvalid ? 'retry-stage-hint' : undefined">
          <legend>处理部分</legend>
          <label v-for="option in stageOptions" :key="option.value" class="option-choice">
            <input v-model="stages" type="checkbox" :value="option.value" :disabled="busy" />
            <span><strong>{{ option.label }}</strong><small>{{ option.description }}</small></span>
          </label>
          <small v-if="partsInvalid" id="retry-stage-hint" class="retry-stage-hint error" role="status">至少选择一个处理部分</small>
        </fieldset>

        <div class="processing-options-actions">
          <button class="quiet" type="button" :disabled="busy" @click="cancel">取消</button>
          <button class="primary" type="submit" :disabled="busy || partsInvalid">{{ busy ? '提交中...' : confirmLabel }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
