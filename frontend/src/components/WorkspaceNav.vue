<script setup lang="ts">
/** 工作区导航：通过显式事件上报页面切换，不直接修改父级状态。 */
import type { NavigationItem, PageId } from '../types'
import { computed, shallowRef } from 'vue'
import WorkspaceMoreDialog from './WorkspaceMoreDialog.vue'

const props = withDefaults(defineProps<{
  pages: NavigationItem[]
  activePage: PageId | null
  docsUrl?: string
}>(), { docsUrl: '/docs/' })

const emit = defineEmits<{
  navigate: [page: PageId]
}>()

const primaryIds: PageId[] = ['search', 'library', 'upload', 'tasks']
const primaryPages = computed(() => props.pages.filter((item) => primaryIds.includes(item.id)))
const morePages = computed(() => props.pages.filter((item) => !primaryIds.includes(item.id)))
const moreActive = computed(() => morePages.value.some((item) => item.id === props.activePage))
const moreOpen = shallowRef(false)
const moreTrigger = shallowRef<HTMLElement | null>(null)

/** 将更多页面的选择交给父级，并关闭当前导航对话框。 */
function navigateFromMore(page: PageId): void {
  moreOpen.value = false
  emit('navigate', page)
}
</script>

<template>
  <aside class="sidebar">
    <nav class="workspace-desktop-nav" aria-label="主导航">
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
    <nav class="workspace-mobile-nav" aria-label="手机主导航">
      <button
        v-for="item in primaryPages"
        :key="item.id"
        type="button"
        :class="{ active: activePage === item.id }"
        :aria-current="activePage === item.id ? 'page' : undefined"
        @click="emit('navigate', item.id)"
      >{{ item.id === 'tasks' ? '任务' : item.label }}</button>
      <button ref="moreTrigger" type="button" :class="{ active: moreActive }" :aria-expanded="moreOpen" aria-haspopup="dialog" @click="moreOpen = true">更多</button>
    </nav>
  </aside>
  <WorkspaceMoreDialog v-if="moreOpen" :pages="morePages" :active-page="activePage" :docs-url="docsUrl" :return-focus="moreTrigger" @close="moreOpen = false" @navigate="navigateFromMore" />
</template>
