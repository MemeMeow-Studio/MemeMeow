<script setup lang="ts">
/** 合集工作区：管理合集列表、详情与成员关系，不复制图片身份。 */
import { onMounted, shallowRef } from 'vue'
import { api } from '../api'
import type { CollectionSummary, MemeImage } from '../types'
import { errorMessage } from '../utils/presentation'
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
    if (selectedCollection.value?.collection_id === item.collection_id) selectedCollection.value.name = name
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
    <div class="section-head">
      <div>
        <h1>{{ selectedCollection ? selectedCollection.name : '合集' }}</h1>
        <p>{{ selectedCollection ? '管理合集成员。删除合集不会删除图片。' : '用合集组织图片库中的资产。' }}</p>
      </div>
      <div v-if="selectedCollection" class="collection-detail-actions">
        <button class="quiet" type="button" :disabled="busy" @click="renameCollection(selectedCollection)">重命名</button>
        <button class="quiet" type="button" :disabled="busy" @click="deleteCollection(selectedCollection)">删除</button>
        <button class="quiet" type="button" @click="backToCollections">返回合集</button>
      </div>
    </div>
    <template v-if="selectedCollection">
      <div class="library-list" role="list">
        <article v-for="item in members" :key="item.meme_id" class="library-row" role="listitem">
          <button class="library-preview-trigger" type="button" :aria-label="`查看 ${item.filename} 图片与元数据`" @click="openImagePreview(item, $event)">
            <img :src="item.media_url" :alt="`预览 ${item.filename}`" loading="lazy" />
          </button>
          <div class="file-meta"><strong :title="item.filename">{{ item.filename }}</strong><small>{{ item.extension }}</small></div>
          <button class="quiet" type="button" :disabled="busy" @click="removeMember(item)">移除</button>
        </article>
        <div v-if="!members.length" class="empty-state compact"><h2>这个合集还没有图片</h2><p>在图片库选择图片后加入合集。</p></div>
      </div>
      <p v-if="notice" class="inline-notice" role="status">{{ notice }}</p>
    </template>
    <template v-else>
      <div class="toolbar collection-toolbar">
        <input v-model="collectionName" aria-label="合集名称" placeholder="新合集名称" @keyup.enter="createCollection" />
        <button class="primary" type="button" :disabled="busy || !collectionName.trim()" @click="createCollection">创建合集</button>
      </div>
      <p v-if="notice" class="inline-notice" role="status">{{ notice }}</p>
      <div class="library-list" role="list">
        <article v-for="item in collections" :key="item.collection_id" class="library-row collection-row" role="listitem">
          <button class="collection-open" type="button" :disabled="busy" @click="openCollection(item)">
            <img v-if="item.cover_media_url" :src="item.cover_media_url" :alt="item.name" />
            <span class="file-meta"><strong>{{ item.name }}</strong><small>{{ item.member_count }} 张图片</small></span>
          </button>
          <div class="collection-actions">
            <button class="quiet" type="button" :disabled="busy" @click="renameCollection(item)">重命名</button>
            <button class="quiet" type="button" :disabled="busy" @click="deleteCollection(item)">删除</button>
          </div>
        </article>
        <div v-if="!collections.length && !busy" class="empty-state compact"><h2>还没有合集</h2><p>创建合集后可从图片库添加成员。</p></div>
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
