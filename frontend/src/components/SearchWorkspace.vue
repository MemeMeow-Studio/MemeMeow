<script setup lang="ts">
/** 检索工作区：拥有检索表单、结果去重与图片复制行为。 */
import { computed, shallowRef } from 'vue'
import { api } from '../api'
import { useImageClipboard } from '../composables/useImageClipboard'
import type { ServiceConfig } from '../types'
import { errorMessage, resultIdentity } from '../utils/presentation'

defineProps<{
  config: ServiceConfig | null
}>()

const emit = defineEmits<{
  error: [message: string]
  clearError: []
}>()

const query = shallowRef('')
const resultCount = shallowRef(8)
const llmEnhance = shallowRef(false)
const results = shallowRef<string[]>([])
const busy = shallowRef(false)
const { copyNotice, copyImage } = useImageClipboard()

const uniqueResults = computed(() => {
  const seen = new Set<string>()
  return results.value.filter((url) => {
    const identity = resultIdentity(url)
    if (!identity || seen.has(identity)) return false
    seen.add(identity)
    return true
  })
})

/** 提交自然语言查询，并在工作区内维护独立加载状态。 */
async function runSearch(): Promise<void> {
  emit('clearError')
  busy.value = true
  try {
    const response = await api.search({
      query: query.value,
      n_results: resultCount.value,
      llm_enhance: llmEnhance.value,
    })
    results.value = response.results
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="workspace search-workspace" :aria-busy="busy">
    <div class="section-head">
      <div>
        <h1>找到合适的表达</h1>
        <p>用一句自然语言描述你想要的情绪或场景。</p>
      </div>
      <span class="cache-pill" :class="{ ready: config }">{{ config ? 'API 已连接' : '等待连接' }}</span>
    </div>
    <form class="search-form" aria-label="自然语言检索" @submit.prevent="runSearch">
      <label class="sr-only" for="search-query">描述想找的表情包</label>
      <input
        id="search-query"
        v-model="query"
        placeholder="例如：开会时发现自己忘记准备材料"
        autocomplete="off"
        autofocus
      />
      <button class="primary" type="submit" :disabled="busy || !query.trim()">
        {{ busy ? '分析中...' : '开始检索' }}
      </button>
    </form>
    <div class="controls">
      <label class="number-control">
        <span>结果数量</span>
        <input v-model.number="resultCount" type="number" min="1" max="30" aria-label="结果数量" />
      </label>
      <label class="switch">
        <input v-model="llmEnhance" type="checkbox" />
        <span class="switch-track" aria-hidden="true"></span>
        <span class="switch-text">使用 LLM 优化语义</span>
      </label>
    </div>
    <template v-if="uniqueResults.length">
      <div class="result-grid">
        <button
          v-for="(url, index) in uniqueResults"
          :key="resultIdentity(url)"
          class="result-item"
          type="button"
          :aria-label="`复制检索结果 ${index + 1}`"
          title="复制图片"
          @click="copyImage(url)"
        >
          <img :src="url" alt="检索结果" loading="lazy" />
        </button>
      </div>
      <p v-if="copyNotice" class="copy-notice" role="status" aria-live="polite">{{ copyNotice }}</p>
    </template>
    <div v-else class="empty-state" :class="{ loading: busy }" role="status" aria-live="polite">
      <h2>{{ busy ? '正在分析你的描述' : '还没有检索结果' }}</h2>
      <p v-if="!busy">输入一句情绪或场景描述后开始。</p>
    </div>
  </section>
</template>
