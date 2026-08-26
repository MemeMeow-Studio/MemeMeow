<script setup lang="ts">
/** 合集工作区：管理合集列表、详情与成员关系，不复制图片身份。 */
import { onMounted, shallowRef } from 'vue'
import { api } from '../api'
import type { CollectionSummary, MemeImage } from '../types'
import { errorMessage, fallbackImageToOriginal, imageDisplayUrl, thumbnailMediaUrl } from '../utils/presentation'
import ImagePreviewDialog from './ImagePreviewDialog.vue'

const emit = defineEmits<{
  error: [message: string]
}>()

const collections = shallowRef<CollectionSummary[]>([])
const selectedCollection = shallowRef<CollectionSummary | null>(null)
const members = shallowRef<MemeImage[]>([])
const collectionName = shallowRef('')
const busy = shallowRef(false)
const notice = shallowRef('')
const previewImage = shallowRef<MemeImage | null>(null)
const previewTrigger = shallowRef<HTMLElement | null>(null)

/** 加载合集列表，供初始进入和修改后刷新。 */
async function loadCollections(): Promise<void> {
  busy.value = true
  try {
    collections.value = (await api.collections()).items
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 创建空合集并刷新列表。 */
async function createCollection(): Promise<void> {
  if (!collectionName.value.trim() || busy.value) return
  busy.value = true
  try {
    await api.createCollection({ name: collectionName.value })
    collectionName.value = ''
    notice.value = '合集已创建'
    await loadCollections()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 通过原生提示修改合集名称，保留现有成员关系。 */
async function renameCollection(item: CollectionSummary): Promise<void> {
  const name = window.prompt('合集名称', item.name)
  if (!name || busy.value) return
  busy.value = true
  try {
    await api.renameCollection(item.collection_id, { name })
    notice.value = '合集已重命名'
    if (selectedCollection.value?.collection_id === item.collection_id) {
      selectedCollection.value = { ...selectedCollection.value, name }
    }
    await loadCollections()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 删除合集但不删除图片，并回到合集列表。 */
async function deleteCollection(item: CollectionSummary): Promise<void> {
  if (busy.value || !window.confirm('删除合集不会删除图片，确定继续吗？')) return
  busy.value = true
  try {
    await api.deleteCollection(item.collection_id)
    if (selectedCollection.value?.collection_id === item.collection_id) selectedCollection.value = null
    notice.value = '合集已删除'
    await loadCollections()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 打开合集详情并读取成员。 */
async function openCollection(item: CollectionSummary): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    selectedCollection.value = await api.collection(item.collection_id)
    members.value = selectedCollection.value?.members || []
    notice.value = ''
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 返回合集列表并刷新可能发生变化的统计。 */
function backToCollections(): void {
  selectedCollection.value = null
  void loadCollections()
}

/** 从当前合集幂等移除一张图片，并刷新详情。 */
async function removeMember(item: MemeImage): Promise<void> {
  if (!selectedCollection.value || busy.value) return
  busy.value = true
  try {
    await api.removeCollectionMember(selectedCollection.value.collection_id, item.meme_id)
    selectedCollection.value = await api.collection(selectedCollection.value.collection_id)
    members.value = selectedCollection.value?.members || []
    notice.value = '图片已从合集移除'
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

/** 打开合集成员图片预览，修复原先无行为的图片按钮。 */
function openImagePreview(item: MemeImage, event: MouseEvent): void {
  previewTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  previewImage.value = item
}

onMounted(loadCollections)
</script>

<template>
  <section class="workspace" :aria-busy="busy">
    <div class="section-head" :class="{ 'collection-section-head': selectedCollection }">
      <div class="collection-heading">
        <button v-if="selectedCollection" class="collection-back" type="button" @click="backToCollections">
          返回合集列表
        </button>
        <h1>{{ selectedCollection ? selectedCollection.name : '合集' }}</h1>
        <p v-if="selectedCollection">
          {{ members.length }} 张图片
        </p>
      </div>
      <div v-if="selectedCollection" class="collection-detail-actions" role="group" aria-label="合集管理操作">
        <button class="quiet" type="button" :disabled="busy" @click="renameCollection(selectedCollection)">重命名</button>
        <button class="quiet collection-danger" type="button" :disabled="busy" @click="deleteCollection(selectedCollection)">删除合集</button>
      </div>
    </div>
    <template v-if="selectedCollection">
      <div class="collection-gallery" role="list" aria-label="合集图片">
        <article v-for="item in members" :key="item.meme_id" class="collection-asset" role="listitem">
          <button class="collection-asset-media" type="button" :aria-label="`查看 ${item.filename} 图片与元数据`" @click="openImagePreview(item, $event)">
            <img :src="imageDisplayUrl(item)" :alt="`预览 ${item.filename}`" loading="lazy" @error="fallbackImageToOriginal($event, item.media_url)" />
          </button>
          <div class="collection-asset-meta">
            <strong :title="item.filename">{{ item.filename }}</strong>
            <small>{{ item.extension || '图片' }}</small>
            <button class="quiet collection-remove" type="button" :disabled="busy" :aria-label="`从合集移除 ${item.filename}`" @click="removeMember(item)">
              移除
            </button>
          </div>
        </article>
        <div v-if="!members.length" class="empty-state compact collection-empty">
          <h2>这个合集还没有图片</h2>
        </div>
      </div>
      <p v-if="notice" class="inline-notice" role="status">{{ notice }}</p>
    </template>
    <template v-else>
      <form class="collection-create-form" aria-label="创建合集" @submit.prevent="createCollection">
        <label class="sr-only" for="collection-name">新合集名称</label>
        <input id="collection-name" v-model="collectionName" placeholder="新合集名称" autocomplete="off" />
        <button class="primary" type="submit" :disabled="busy || !collectionName.trim()">
          {{ busy ? '创建中...' : '创建合集' }}
        </button>
      </form>
      <p v-if="notice" class="inline-notice" role="status">{{ notice }}</p>
      <div v-if="busy && !collections.length" class="empty-state compact collection-loading" role="status">
        <h2>正在加载合集</h2>
      </div>
      <div v-else class="collection-list" role="list" aria-label="合集列表">
        <article v-for="item in collections" :key="item.collection_id" class="collection-row" role="listitem">
          <button class="collection-open" type="button" :disabled="busy" :aria-label="`打开合集 ${item.name}`" @click="openCollection(item)">
            <span class="collection-cover" aria-hidden="true">
              <img
                v-if="item.cover_media_url"
                :src="thumbnailMediaUrl(item.cover_media_url, item.cover_thumbnail)"
                alt=""
                loading="lazy"
                @error="fallbackImageToOriginal($event, item.cover_media_url)"
              />
              <span v-else>无图</span>
            </span>
            <span class="collection-summary">
              <strong :title="item.name">{{ item.name }}</strong>
              <span class="collection-count">{{ item.member_count ?? 0 }} 张图片</span>
            </span>
          </button>
          <div class="collection-actions" role="group" aria-label="合集操作">
            <button class="quiet" type="button" :disabled="busy" @click="renameCollection(item)">重命名</button>
            <button class="quiet collection-danger" type="button" :disabled="busy" @click="deleteCollection(item)">删除合集</button>
          </div>
        </article>
        <div v-if="!collections.length && !busy" class="empty-state compact"><h2>还没有合集</h2></div>
      </div>
    </template>
  </section>

  <ImagePreviewDialog
    v-if="previewImage"
    :image="previewImage"
    :return-focus="previewTrigger"
    @close="previewImage = null"
  />
</template>
