<script setup lang="ts">
/** 检索工作区：拥有检索表单、结果去重与图片复制行为。 */
import { computed, shallowRef } from 'vue'
import { api } from '../api'
import { useImageClipboard } from '../composables/useImageClipboard'
import type { SearchResponse, SearchResultMedia, ServiceConfig } from '../types'
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
const resultMedia = shallowRef<SearchResultMedia[]>([])
const originalLoaded = shallowRef(new Set<string>())
const originalFailed = shallowRef(new Set<string>())
const thumbnailFailed = shallowRef(new Set<string>())
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

const searchItems = computed(() => {
  const media = resultMedia.value
  const usedMedia = new Set<number>()
  return uniqueResults.value.map((url, index) => {
    const identity = resultIdentity(url)
    const ordered = media[index]
    const orderedMatches = ordered && resultIdentity(ordered.media_url) === identity
    const mediaIndex = orderedMatches
      ? index
      : media.findIndex((candidate, candidateIndex) => !usedMedia.has(candidateIndex) && resultIdentity(candidate.media_url) === identity)
    const candidate = mediaIndex >= 0 ? media[mediaIndex] : undefined
    if (mediaIndex >= 0) usedMedia.add(mediaIndex)
    return {
      meme_id: candidate?.meme_id || identity || `result-${index}`,
      media_url: candidate?.media_url || url,
      thumbnail: candidate?.thumbnail,
    }
  })
})

/** 判断旁路缩略图是否可用于当前结果的初始展示。 */
function hasThumbnail(item: SearchResultMedia): boolean {
  return item.thumbnail?.status === 'available' && !!item.thumbnail.media_url && !thumbnailFailed.value.has(item.meme_id)
}

/** 记录一个结果集合状态并用新 Set 触发浅层响应式更新。 */
function addResultState(target: typeof originalLoaded, key: string): void {
  if (target.value.has(key)) return
  const next = new Set(target.value)
  next.add(key)
  target.value = next
}

/** 返回当前结果图片层，原图成功后替换缩略图，失败时保留缩略图。 */
function resultSource(item: SearchResultMedia): string {
  if (originalLoaded.value.has(item.meme_id) && !originalFailed.value.has(item.meme_id)) return item.media_url
  if (hasThumbnail(item)) return item.thumbnail?.media_url || item.media_url
  return item.media_url
}

/** 让浏览器尽早并行请求原图；成功后由 visible 图片切换到原图。 */
function shouldPreloadOriginal(item: SearchResultMedia): boolean {
  return hasThumbnail(item) && !originalLoaded.value.has(item.meme_id) && !originalFailed.value.has(item.meme_id)
}

/** 原图预加载成功时更新对应结果，不影响其它结果的加载状态。 */
function markOriginalLoaded(item: SearchResultMedia): void {
  addResultState(originalLoaded, item.meme_id)
}

/** 原图预加载失败时保留仍可用的缩略图。 */
function markOriginalFailed(item: SearchResultMedia): void {
  addResultState(originalFailed, item.meme_id)
}

/** 缩略图失败时回退原图；原图失败则保持缩略图或当前空状态。 */
function handleResultImageError(event: Event, item: SearchResultMedia): void {
  const image = event.currentTarget as HTMLImageElement | null
  if (!image) return
  const source = image.getAttribute('src') || ''
  if (item.thumbnail?.media_url && source === item.thumbnail.media_url) {
    addResultState(thumbnailFailed, item.meme_id)
    if (!originalFailed.value.has(item.meme_id)) image.src = item.media_url
    return
  }
  if (source === item.media_url) addResultState(originalFailed, item.meme_id)
}

/** 清理上一次检索的结果媒体关联和渐进加载状态。 */
function resetResultState(): void {
  resultMedia.value = []
  originalLoaded.value = new Set()
  originalFailed.value = new Set()
  thumbnailFailed.value = new Set()
}

/** 提交自然语言查询，并在工作区内维护独立加载状态。 */
async function runSearch(): Promise<void> {
  emit('clearError')
  busy.value = true
  try {
    const response = await api.search({
      query: query.value,
      n_results: resultCount.value,
      llm_enhance: llmEnhance.value,
    }) as SearchResponse
    results.value = Array.isArray(response.results) ? response.results : []
    resultMedia.value = Array.isArray(response.result_media) ? response.result_media : []
    originalLoaded.value = new Set()
    originalFailed.value = new Set()
    thumbnailFailed.value = new Set()
  } catch (reason) {
    resetResultState()
    results.value = []
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
          v-for="(item, index) in searchItems"
          :key="`${item.meme_id}:${index}`"
          class="result-item"
          type="button"
          :aria-label="`复制检索结果 ${index + 1}`"
          title="复制图片"
          @click="copyImage(item.media_url)"
        >
          <img :src="resultSource(item)" alt="检索结果" loading="lazy" @error="handleResultImageError($event, item)" />
          <img
            v-if="shouldPreloadOriginal(item)"
            class="result-original-preload"
            :src="item.media_url"
            alt=""
            aria-hidden="true"
            @load="markOriginalLoaded(item)"
            @error="markOriginalFailed(item)"
          />
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
