<script setup lang="ts">
/** 工作区导航：通过显式事件上报页面切换，不直接修改父级状态。 */
import type { NavigationItem, PageId } from '../types'

defineProps<{
  pages: NavigationItem[]
  activePage: PageId
}>()

const emit = defineEmits<{
  navigate: [page: PageId]
}>()
</script>

<template>
  <aside class="sidebar">
    <nav aria-label="工作区">
      <button
        v-for="item in pages"
        :key="item.id"
        type="button"
        :class="{ active: activePage === item.id }"
        :aria-current="activePage === item.id ? 'page' : undefined"
        @click="emit('navigate', item.id)"
      >
        {{ item.label }}
      </button>
    </nav>
  </aside>
</template>
