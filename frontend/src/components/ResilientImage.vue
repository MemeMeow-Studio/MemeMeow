<script setup lang="ts">
/** 可回退图片：先加载展示层，失败后尝试原图，二者都失败时显示受控占位。 */
import { shallowRef, watch } from 'vue'

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<{
  src: string
  fallbackSrc?: string
  alt?: string
  loading?: 'eager' | 'lazy'
  fallbackLabel?: string
}>(), {
  fallbackSrc: '',
  alt: '',
  loading: 'lazy',
  fallbackLabel: '图片暂不可用',
})

const currentSrc = shallowRef(props.src)
const loadFailed = shallowRef(!props.src)

/** 媒体地址或回退地址变化时清除旧失败状态，支持列表刷新后重新尝试。 */
watch(
  () => [props.src, props.fallbackSrc] as const,
  ([src]) => {
    currentSrc.value = src
    loadFailed.value = !src
  },
)

/** 首次失败切换到原图，原图也失败后进入显式占位状态。 */
function handleError(): void {
  const fallback = props.fallbackSrc.trim()
  if (fallback && currentSrc.value !== fallback) {
    currentSrc.value = fallback
    return
  }
  loadFailed.value = true
}
</script>

<template>
  <span class="resilient-image" :class="{ 'resilient-image-failed': loadFailed }">
    <img v-if="!loadFailed" :src="currentSrc" :alt="props.alt" :loading="props.loading" v-bind="$attrs" @error="handleError" />
    <span v-else class="resilient-image-placeholder" role="img" :aria-label="props.fallbackLabel">{{ props.fallbackLabel }}</span>
  </span>
</template>
