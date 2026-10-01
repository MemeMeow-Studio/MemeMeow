<script setup lang="ts">
/** 公共服务状态展示：供桌面顶栏和手机页面标题区域读取同一份状态。 */
import type { ServiceConfig } from '../types'

defineProps<{
  config: ServiceConfig | null
  embeddingState: string
  embeddingStateLabel: string
}>()
</script>

<template>
  <div class="service-state" role="status" aria-live="polite">
    <span class="pulse" :class="`pulse-${embeddingState}`" aria-hidden="true"></span>
    <span class="service-model" :title="config?.embedding_model || '服务配置加载中'">
      {{ config?.embedding_model || '服务配置加载中' }}
    </span>
    <span class="service-divider" aria-hidden="true"></span>
    <span class="embedding-global" :class="embeddingState">{{ embeddingStateLabel }}</span>
  </div>
</template>
