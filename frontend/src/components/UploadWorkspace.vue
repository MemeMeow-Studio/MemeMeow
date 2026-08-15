<script setup lang="ts">
/** 上传工作区：管理文件选择、自动命名选项与逐文件结果。 */
import { shallowRef } from 'vue'
import { api } from '../api'
import type { UploadResult } from '../types'
import { errorMessage } from '../utils/presentation'

const emit = defineEmits<{
  error: [message: string]
  clearError: []
  openTask: [taskId: string]
}>()

const files = shallowRef<File[]>([])
const autoName = shallowRef(false)
const uploadResults = shallowRef<UploadResult[]>([])
const busy = shallowRef(false)

/** 读取原生文件输入，并以不可变数组保存用户选择。 */
function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  files.value = [...(input.files || [])]
}

/** 上传当前文件并保存逐文件结果，失败时上报壳层错误。 */
async function upload(): Promise<void> {
  if (!files.value.length) return
  emit('clearError')
  busy.value = true
  try {
    uploadResults.value = (await api.upload(files.value, autoName.value)).results
    files.value = []
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="workspace narrow" :aria-busy="busy">
    <div class="section-head">
      <div><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF。</p></div>
    </div>
    <div class="upload-panel">
      <label class="drop-zone">
        <input type="file" multiple accept=".png,.jpg,.jpeg,.gif" aria-label="选择图片文件" @change="onFiles" />
        <span class="drop-title">选择图片文件</span>
        <span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件` : '点击选择或拖入文件' }}</span>
      </label>
      <label class="switch">
        <input v-model="autoName" type="checkbox" />
        <span class="switch-track" aria-hidden="true"></span>
        <span class="switch-text">处理完成后按标题自动命名</span>
      </label>
      <button class="primary wide" type="button" :disabled="busy || !files.length" @click="upload">
        {{ busy ? '上传中...' : '上传所选图片' }}
      </button>
    </div>
    <div v-if="uploadResults.length" class="upload-results" aria-live="polite">
      <div v-for="(item, index) in uploadResults" :key="item.meme_id || `${item.filename}-${index}`" class="upload-result" :class="{ fail: !item.ok }">
        <span>{{ item.ok ? '完成' : '失败' }}</span>
        <strong :title="item.filename">{{ item.filename }}</strong>
        <button v-if="item.metadata_job_id" class="quiet" type="button" @click="emit('openTask', item.metadata_job_id)">查看任务</button>
        <small v-else>{{ item.ok ? item.saved_filename : item.error }}</small>
      </div>
    </div>
  </section>
</template>
