<script setup lang="ts">
/** 图片库状态与每页数量控件；查询和选择清理由工作区处理。 */
import { libraryPageSizes, type ImageLibraryStatus } from "../../utils/libraryPreferences"

const props = defineProps<{
  status: ImageLibraryStatus
  pageSize: number
  disabled: boolean
}>()

const emit = defineEmits<{
  "update:status": [value: ImageLibraryStatus]
  "update:page-size": [value: number]
}>()

/** 将状态选项交给工作区，供服务端筛选请求使用。 */
function updateStatus(event: Event): void {
  emit("update:status", (event.target as HTMLSelectElement).value as ImageLibraryStatus)
}

/** 将每页数量转换为数字后交给工作区保存并重新查询。 */
function updatePageSize(event: Event): void {
  emit("update:page-size", Number((event.target as HTMLSelectElement).value))
}
</script>

<template>
  <select :value="props.status" aria-label="图片状态类别" :disabled="props.disabled" @change="updateStatus">
    <option value="all">全部状态</option>
    <option value="ready">全部就绪</option>
    <option value="unready">未就绪</option>
    <option value="context_unready">语境未就绪</option>
    <option value="text_unready">文本向量未就绪</option>
    <option value="visual_unready">视觉向量未就绪</option>
  </select>
  <select :value="props.pageSize" aria-label="每页图片数量" :disabled="props.disabled" @change="updatePageSize">
    <option v-for="size in libraryPageSizes" :key="size" :value="size">每页 {{ size }} 张</option>
  </select>
</template>
