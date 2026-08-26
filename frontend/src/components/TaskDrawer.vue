<script setup lang="ts">
/** 任务详情抽屉：展示任务公共字段，并提供一致的模态键盘闭环。 */
import { computed, shallowRef } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { TaskItem } from '../types'
import { formatTaskTime, imageStageLabel, imageStageStatusLabel, taskActivity, taskStatusLabel, taskTypeLabel } from '../utils/presentation'

const props = defineProps<{
  task: TaskItem
  retrying?: boolean
  returnFocus?: HTMLElement | null
}>()

const emit = defineEmits<{
  close: []
  retry: []
  'retry-stage': []
  'retry-full': []
}>()

const drawer = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)
const activity = computed(() => taskActivity(props.task))
const isImageStage = computed(() => ['visual_embedding_generation', 'meme_context_generation', 'image_auto_rename', 'text_embedding_generation'].includes(props.task.task_type))
const taskStage = computed(() => props.task.image_stage || (props.task.task_type === 'image_auto_rename' ? 'auto_rename' : null))
const autoRenameRetryable = computed(() => {
  if (taskStage.value !== 'auto_rename' || props.task.status !== 'failed' || props.task.read_only) return false
  if (props.task.image_stage_recoverable === false) return false
  return props.task.image_stage_recoverable === true || props.task.image_stage_status === 'warning'
})
const canRetryStage = computed(() => {
  if (!isImageStage.value || props.task.status !== 'failed' || props.task.read_only) return false
  if (props.task.task_type === 'image_auto_rename' || taskStage.value === 'auto_rename') return autoRenameRetryable.value
  return props.task.submission_mode === 'standalone'
})
const canRetryFull = computed(() => props.task.submission_mode === 'pipeline' && props.task.task_type !== 'image_auto_rename' && taskStage.value !== 'auto_rename' && !!props.task.processing_job_id && ['failed', 'blocked', 'unknown_execution'].includes(props.task.status))
const canRetryLegacy = computed(() => props.task.task_type === 'meme_context_generation' && props.task.status === 'failed' && props.task.submission_mode == null && !props.task.read_only)

useModalDialog({
  dialog: drawer,
  initialFocus: closeButton,
  returnFocus: props.returnFocus,
  close: () => emit('close'),
})
</script>

<template>
  <div class="task-drawer-backdrop" role="presentation" @click.self="emit('close')">
    <aside ref="drawer" class="task-drawer" role="dialog" aria-modal="true" aria-label="任务详情" tabindex="-1">
      <div class="drawer-head">
        <h2>任务详情</h2>
        <button ref="closeButton" class="quiet" type="button" aria-label="关闭任务详情" @click="emit('close')">关闭</button>
      </div>
      <dl>
        <div><dt>状态</dt><dd>{{ taskStatusLabel(task.status) }}</dd></div>
        <div><dt>类型</dt><dd>{{ taskTypeLabel(task.task_type) }}</dd></div>
        <div><dt>阶段</dt><dd>{{ taskStage ? `${imageStageLabel(taskStage)}：${imageStageStatusLabel(task.image_stage_status || (autoRenameRetryable ? 'warning' : task.status))}` : task.message || '—' }}</dd></div>
        <div v-if="activity" class="task-activity-detail">
          <dt>Agent 工作回合</dt>
          <dd>
            <strong>{{ activity.turns }}</strong>
            <time>最近活动 {{ activity.lastActivity }}</time>
          </dd>
        </div>
        <div><dt>创建时间</dt><dd>{{ formatTaskTime(task.created_at) }}</dd></div>
        <div><dt>完成时间</dt><dd>{{ formatTaskTime(task.completed_at) }}</dd></div>
        <div v-if="task.error"><dt>错误</dt><dd>{{ task.error.error }}</dd></div>
        <div v-if="task.result?.auto_named !== undefined">
          <dt>自动命名</dt>
          <dd>{{ task.result.auto_named ? task.result.saved_filename : task.result.auto_name_error || '未执行' }}</dd>
        </div>
      </dl>
      <button
        v-if="canRetryFull"
        class="primary"
        type="button"
        :disabled="retrying"
        @click="emit('retry-full')"
      >
        {{ retrying ? '重试中...' : '完整重试' }}
      </button>
      <button
        v-if="canRetryStage"
        class="quiet"
        type="button"
        :disabled="retrying"
        @click="emit('retry-stage')"
      >
        {{ retrying ? '提交中...' : taskStage === 'auto_rename' ? '恢复自动命名' : '仅重试本阶段' }}
      </button>
      <button
        v-if="canRetryLegacy"
        class="primary"
        type="button"
        :disabled="retrying"
        @click="emit('retry')"
      >
        {{ retrying ? '重试中...' : '重试' }}
      </button>
    </aside>
  </div>
</template>
