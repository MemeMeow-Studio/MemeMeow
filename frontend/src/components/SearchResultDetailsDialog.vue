<script setup lang="ts">
/** 搜索结果轻量详情：展示本次检索匹配度，并把完整信息交给图片库详情组件。 */
import { shallowRef } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'

const props = defineProps<{
  memeId: string
  mediaUrl: string
  score?: number
  returnFocus?: HTMLElement | null
}>()

const emit = defineEmits<{
  close: []
  'open-library': []
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)
const scoreLabel = Number.isFinite(props.score) ? props.score!.toFixed(4) : '不可用'

useModalDialog({ dialog, initialFocus: closeButton, returnFocus: props.returnFocus, close: () => emit('close') })
</script>

<template>
  <div class="image-dialog-backdrop" role="presentation" @click.self="emit('close')">
    <section ref="dialog" class="search-result-dialog" role="dialog" aria-modal="true" aria-labelledby="search-result-dialog-title" tabindex="-1">
      <header class="image-dialog-head">
        <h2 id="search-result-dialog-title">检索结果详情</h2>
        <button ref="closeButton" class="quiet" type="button" aria-label="关闭检索结果详情" @click="emit('close')">关闭</button>
      </header>
      <div class="search-result-dialog-body">
        <img :src="mediaUrl" alt="检索结果图片" />
        <dl>
          <div><dt>Embedding 匹配度</dt><dd>{{ scoreLabel }}</dd></div>
          <div><dt>Meme ID</dt><dd>{{ memeId }}</dd></div>
        </dl>
      </div>
      <footer class="search-result-dialog-actions">
        <button class="primary" type="button" @click="emit('open-library')">查看图片库详情</button>
      </footer>
    </section>
  </div>
</template>
