<script setup lang="ts">
/** 本次上传结果列表：按文件顺序展示传输、保存结果和可用操作。 */
import type { UploadBatchItem } from '../composables/useUploadBatch'
import { useUploadRecords } from '../composables/useUploadRecords'
import { useToast } from '../composables/useToast'

const props = withDefaults(defineProps<{ items: UploadBatchItem[]; busy: boolean; storageScope?: string }>(), { storageScope: 'local' })
const emit = defineEmits<{ retryTransfer: [requestId: string] }>()
const { rows, summary, error, loading, acting, begin, refresh, act } = useUploadRecords(() => props.items, props.storageScope)
const notifications = useToast()
defineExpose({ begin, refresh })

/** 手动刷新本次上传结果，内部轮询保持原有页面状态反馈。 */
function refreshResults(): void {
  if (loading.value) return
  notifications.show('开始刷新上传结果', 'info')
  void refresh()
}
</script>

<template>
  <section v-if="rows.length" class="upload-records" aria-label="本次上传结果">
    <div class="upload-records-heading">
      <h2>本次上传</h2>
      <button class="quiet" type="button" :disabled="loading" @click="refreshResults">刷新结果</button>
    </div>
    <p class="upload-records-summary" aria-live="polite">共 {{ summary.total }} 个，上传成功 {{ summary.success }} 个，失败 {{ summary.failed }} 个，取消 {{ summary.cancelled }} 个</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul class="upload-record-list">
      <li v-for="row in rows" :key="row.requestId" class="upload-record" :class="row.state">
        <div class="upload-record-name"><strong>{{ row.filename }}</strong><p v-if="row.detail">{{ row.detail }}</p><small v-if="row.errorCode">错误码：{{ row.errorCode }}</small></div>
        <div class="upload-record-state"><span>{{ row.label }}</span><progress v-if="row.progress !== null" :value="row.progress" max="1" :aria-label="`${row.filename} 上传进度`" /></div>
        <div class="upload-record-actions">
          <button v-if="row.task?.retryable" class="quiet" type="button" :disabled="acting.has(row.task.task_id)" @click="act(row.task, 'retry')">重试上传</button>
          <button v-if="row.canRetryTransfer" class="quiet" type="button" :disabled="props.busy" @click="emit('retryTransfer', row.requestId)">重试上传</button>
          <button v-if="row.task && ['queued', 'running'].includes(row.task.status)" class="quiet" type="button" :disabled="acting.has(row.task.task_id)" @click="act(row.task, 'cancel')">取消上传</button>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.upload-records { margin-top: 24px; }
.upload-records-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.upload-records-heading h2 { margin: 0; font-size: 20px; }
.upload-records-summary { color: var(--muted); font-size: 14px; }
.upload-record-list { display: grid; gap: 10px; list-style: none; margin: 0; padding: 0; }
.upload-record { display: grid; grid-template-columns: minmax(0, 1fr) minmax(140px, 180px) 110px; align-items: center; gap: 16px; padding: 14px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface, #fff); }
.upload-record-name { min-width: 0; overflow-wrap: anywhere; }
.upload-record-name strong { font-size: 15px; }
.upload-record-name p, .upload-record-name small { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
.upload-record-state { display: grid; gap: 8px; font-size: 14px; }
.upload-record-state progress { width: 100%; }
.upload-record-actions { display: flex; justify-content: flex-end; }
.upload-record-actions button { padding: 8px 12px; white-space: nowrap; }
.success .upload-record-state { color: var(--success); }
.failed .upload-record-state { color: var(--error); }
@media (max-width: 640px) {
  .upload-record { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .upload-record-actions { justify-content: flex-start; }
}
</style>
