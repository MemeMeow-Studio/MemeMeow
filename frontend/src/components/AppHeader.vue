<script setup lang="ts">
/** 工作台顶栏：展示品牌与服务连接状态，不持有业务状态。 */
import type { ServiceConfig } from '../types'
import ServiceStatus from './ServiceStatus.vue'

defineProps<{
  config: ServiceConfig | null
  embeddingState: string
  embeddingStateLabel: string
}>()
</script>

<template>
  <header class="topbar">
    <div class="brand" aria-label="MemeMeow">
      <span class="brand-mark" aria-hidden="true">M</span>
      <div><strong>MemeMeow</strong></div>
    </div>
    <ServiceStatus class="desktop-service-state" :config="config" :embedding-state="embeddingState" :embedding-state-label="embeddingStateLabel" />
  </header>
</template>
