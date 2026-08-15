<script setup lang="ts">
/** 加入合集对话框：仅呈现表单，通过 typed model 与事件交给图库工作区执行。 */
import { shallowRef } from 'vue'
import { useModalDialog } from '../composables/useModalDialog'
import type { CollectionSummary } from '../types'

const props = defineProps<{
  collections: CollectionSummary[]
  selectedCount: number
  busy: boolean
  returnFocus?: HTMLElement | null
}>()

const emit = defineEmits<{
  close: []
  add: []
  createAndAdd: []
}>()

const target = defineModel<string>('target', { required: true })
const name = defineModel<string>('name', { required: true })
const dialog = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)

useModalDialog({
  dialog,
  initialFocus: closeButton,
  returnFocus: props.returnFocus,
  close: () => { if (!props.busy) emit('close') },
})
</script>

<template>
  <div class="image-dialog-backdrop" role="presentation" @click.self="!busy && emit('close')">
    <section ref="dialog" class="image-dialog compact-dialog" role="dialog" aria-modal="true" aria-label="加入合集" tabindex="-1">
      <header class="image-dialog-head">
        <h2>加入合集</h2>
        <button ref="closeButton" class="quiet" type="button" :disabled="busy" @click="emit('close')">关闭</button>
      </header>
      <div class="collection-form">
        <p>已选择 {{ selectedCount }} 张图片</p>
        <label class="field">
          <span>目标合集</span>
          <select v-model="target" :disabled="busy">
            <option value="">请选择合集</option>
            <option v-for="item in collections" :key="item.collection_id" :value="item.collection_id">{{ item.name }}</option>
          </select>
        </label>
        <button class="primary wide" type="button" :disabled="busy || !target" @click="emit('add')">
          {{ busy ? '加入中...' : '确认加入' }}
        </button>
        <div class="collection-inline-create">
          <label class="field">
            <span>或新建合集</span>
            <input v-model="name" aria-label="新建合集名称" placeholder="新合集名称" :disabled="busy" />
          </label>
          <button class="quiet wide" type="button" :disabled="busy || !name.trim()" @click="emit('createAndAdd')">
            创建并加入
          </button>
        </div>
      </div>
    </section>
  </div>
</template>
