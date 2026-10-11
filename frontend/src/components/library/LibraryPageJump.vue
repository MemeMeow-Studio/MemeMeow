<script setup lang="ts">
/** 图片库页码输入：校验目标页码，并将跳转请求交给工作区。 */
import { computed, shallowRef, watch } from 'vue'

const props = defineProps<{
  page: number
  pageCount: number
  disabled: boolean
}>()

const emit = defineEmits<{
  changePage: [page: number]
}>()

const pageInput = shallowRef<number | string>(props.page)
const targetPage = computed(() => Number(pageInput.value))
const canJump = computed(() => !props.disabled
  && Number.isInteger(targetPage.value)
  && targetPage.value >= 1
  && targetPage.value <= props.pageCount
  && targetPage.value !== props.page)

watch(() => props.page, nextPage => { pageInput.value = nextPage })

/** 表单提交时发送范围内的整数页码，供工作区加载图片并清理选择。 */
function submitPage(): void {
  if (canJump.value) emit('changePage', targetPage.value)
}
</script>

<template>
  <form class="library-page-jump" aria-label="跳转图片库页码" @submit.prevent="submitPage">
    <label class="library-page-jump-label">
      <span>跳至</span>
      <input
        v-model="pageInput"
        class="library-page-jump-input"
        type="number"
        aria-label="目标页码"
        min="1"
        :max="pageCount"
        step="1"
        required
        :disabled="disabled"
      />
      <span>页</span>
    </label>
    <button class="quiet" type="submit" :disabled="!canJump">跳转</button>
  </form>
</template>

<style scoped>
.library-page-jump,
.library-page-jump-label {
  display: flex;
  align-items: center;
  gap: 8px;
}

.library-page-jump-input {
  width: 5rem;
  min-width: 0;
}
</style>
