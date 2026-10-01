<script setup lang="ts">
/** 图片库展示方式控件：通过 v-model 在方块和列表之间切换，图片选择由工作区保留。 */
const view = defineModel<'grid' | 'list'>({ required: true })
</script>

<template>
  <div class="library-view-toggle" role="group" aria-label="图片展示方式">
    <button class="quiet" type="button" :aria-pressed="view === 'grid'" @click="view = 'grid'">方块</button>
    <button class="quiet" type="button" :aria-pressed="view === 'list'" @click="view = 'list'">列表</button>
  </div>
</template>

<style scoped>
.library-view-toggle { display: flex; gap: 6px; flex-shrink: 0; }
.library-view-toggle button[aria-pressed="true"] { color: var(--accent-deep); border-color: var(--accent); background: var(--surface-active); }
</style>
