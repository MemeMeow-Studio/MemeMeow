<script setup lang="ts">
/** 手机导航的更多入口：保留次级页面和文档，并共享模态焦点控制。 */
import { shallowRef } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { NavigationItem, PageId } from '../types'

const props = defineProps<{
  pages: NavigationItem[]
  activePage: PageId | null
  docsUrl: string
  returnFocus: HTMLElement | null
}>()

const emit = defineEmits<{ close: []; navigate: [page: PageId] }>()
const dialog = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)

useModalDialog({ dialog, initialFocus: closeButton, returnFocus: props.returnFocus, close: () => emit('close') })
</script>

<template>
  <Teleport to="body">
    <div class="image-dialog-backdrop workspace-more-backdrop" role="presentation" @click.self="emit('close')">
      <section ref="dialog" class="image-dialog compact-dialog workspace-more-dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-more-title" tabindex="-1">
        <header class="image-dialog-head">
          <h2 id="workspace-more-title">更多</h2>
          <button ref="closeButton" class="quiet" type="button" @click="emit('close')">关闭</button>
        </header>
        <nav class="workspace-more-links" aria-label="更多页面">
          <button v-for="item in pages" :key="item.id" class="quiet" type="button" :class="{ active: activePage === item.id }" :aria-current="activePage === item.id ? 'page' : undefined" @click="emit('navigate', item.id)">{{ item.label }}</button>
          <a class="quiet" :href="docsUrl" target="_blank" rel="noopener noreferrer">使用文档<span class="sr-only">（在新窗口打开）</span></a>
        </nav>
      </section>
    </div>
  </Teleport>
</template>
