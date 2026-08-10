<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, pollTask } from './api'

const page = ref('search')
const pages = [
  { id: 'search', label: '检索', mark: '⌕' },
  { id: 'library', label: '图片库', mark: '▦' },
  { id: 'upload', label: '上传', mark: '↑' },
  { id: 'label', label: '标注', mark: '✎' },
]
const busy = ref(false)
const error = ref('')
const config = ref(null)
const task = ref(null)

const query = ref('')
const resultCount = ref(8)
const llmEnhance = ref(false)
const results = ref([])

const directory = ref('')
const directories = ref([])
const images = ref([])
const filter = ref('')
const newDirectory = ref('')

const files = ref([])
const autoName = ref(false)
const uploadResults = ref([])

const labelDirectory = ref('')
const labelFilename = ref('')
const candidates = ref([])
const customName = ref('')

const selectedImage = computed(() => images.value.find((item) => item.filename === labelFilename.value))

function clearError() { error.value = '' }
function showError(reason) { error.value = reason?.message || '请求失败' }

async function runSearch() {
  clearError(); busy.value = true
  try { results.value = (await api.search({ query: query.value, n_results: resultCount.value, llm_enhance: llmEnhance.value })).results } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function loadLibrary() {
  clearError(); busy.value = true
  try {
    const data = await api.images({ directory: directory.value, search: filter.value })
    images.value = data.items; directories.value = data.directories || []
  } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function loadLabelLibrary() {
  clearError(); busy.value = true
  try {
    const data = await api.images({ directory: labelDirectory.value })
    images.value = data.items; directories.value = data.directories || []; labelFilename.value = ''; candidates.value = []; customName.value = ''
  } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function makeDirectory() {
  if (!newDirectory.value.trim()) return
  try { await api.createDirectory({ name: newDirectory.value.trim(), parent: directory.value }); newDirectory.value = ''; await loadLibrary() } catch (reason) { showError(reason) }
}

async function rename(item) {
  const name = window.prompt('新文件名', item.filename)
  if (!name) return
  try { await api.rename({ directory: directory.value, filename: item.filename, new_name: name }); await loadLibrary() } catch (reason) { showError(reason) }
}

function onFiles(event) { files.value = [...event.target.files] }
async function upload() {
  if (!files.value.length) return
  clearError(); busy.value = true
  try { uploadResults.value = (await api.upload(directory.value, files.value, autoName.value)).results; files.value = [] } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function describe() {
  if (!labelFilename.value) return
  clearError(); busy.value = true
  try { candidates.value = (await api.describe({ directory: labelDirectory.value, filename: labelFilename.value })).candidates } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function submitLabel() {
  if (!customName.value.trim() || !labelFilename.value) return
  try {
    const renamed = await api.rename({ directory: labelDirectory.value, filename: labelFilename.value, new_name: customName.value.trim() })
    await loadLabelLibrary(); labelFilename.value = renamed.filename
  } catch (reason) { showError(reason) }
}

async function selectLabelImage(filename) { labelFilename.value = filename; candidates.value = []; customName.value = '' }
function navigateLabel(offset) {
  const index = images.value.findIndex((item) => item.filename === labelFilename.value)
  const target = images.value[index + offset]
  if (target) selectLabelImage(target.filename)
}
async function startBatchLabel() {
  if (!images.value.length) return
  try {
    const submitted = await api.batchLabel(images.value.map((item) => ({ directory: labelDirectory.value, filename: item.filename })))
    task.value = await pollTask(submitted.task_id, (next) => { task.value = next })
  } catch (reason) { showError(reason) }
}
async function generateCache() {
  try {
    const submitted = await api.generateCache(); task.value = submitted
    task.value = await pollTask(submitted.task_id, (next) => { task.value = next })
  } catch (reason) { showError(reason) }
}

function changePage(next) { page.value = next; clearError(); if (next === 'library') loadLibrary(); if (next === 'label') loadLabelLibrary() }
onMounted(async () => { try { config.value = await api.config() } catch (reason) { showError(reason) } })
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark">M</span><div><strong>MemeMeow</strong><small>本地图片工作台</small></div></div>
      <div class="service-state"><span class="pulse"></span>{{ config?.embedding_model || '服务配置加载中' }}</div>
    </header>
    <div class="body">
      <aside class="sidebar">
        <nav><button v-for="item in pages" :key="item.id" :class="{ active: page === item.id }" @click="changePage(item.id)"><span>{{ item.mark }}</span>{{ item.label }}</button></nav>
        <div class="sidebar-foot"><button class="cache-button" @click="generateCache">生成检索缓存</button><div v-if="task" class="task-line"><span :class="`dot ${task.status}`"></span>{{ task.message || task.status }}<b v-if="task.progress != null">{{ Math.round(task.progress * 100) }}%</b></div></div>
      </aside>
      <main class="content">
        <div v-if="error" class="error-banner">{{ error }}<button @click="clearError">×</button></div>
        <section v-if="page === 'search'" class="workspace">
          <div class="section-head"><div><span class="eyebrow">SEMANTIC SEARCH</span><h1>找到合适的表达</h1><p>用一句自然语言描述你想要的情绪或场景。</p></div><span class="cache-pill" :class="{ ready: config }">{{ config ? 'API 已连接' : '等待连接' }}</span></div>
          <form class="search-form" @submit.prevent="runSearch"><input v-model="query" placeholder="例如：开会时发现自己忘记准备材料" autofocus /><button class="primary" :disabled="busy || !query.trim()">{{ busy ? '检索中...' : '开始检索' }}</button></form>
          <div class="controls"><label>结果数量 <input v-model.number="resultCount" type="number" min="1" max="30" /></label><label class="switch"><input v-model="llmEnhance" type="checkbox" /><span></span>使用 LLM 优化语义</label></div>
          <div v-if="results.length" class="result-grid"><a v-for="url in results" :key="url" class="result-item" :href="url" target="_blank"><img :src="url" alt="检索结果" loading="lazy" /></a></div>
          <div v-else class="empty-state"><div class="empty-icon">⌕</div><h2>{{ busy ? '正在分析你的描述' : '还没有检索结果' }}</h2><p>输入描述后，结果会显示在这里。</p></div>
        </section>
        <section v-else-if="page === 'library'" class="workspace">
          <div class="section-head"><div><span class="eyebrow">IMAGE LIBRARY</span><h1>图片库</h1><p>浏览、筛选和整理本地图片。</p></div></div>
          <div class="toolbar"><select v-model="directory" @change="loadLibrary"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="directory ? `${directory}/${item}` : item">{{ item }}</option></select><input v-model="filter" placeholder="筛选文件名" @keyup.enter="loadLibrary" /><button @click="loadLibrary">刷新</button><input v-model="newDirectory" placeholder="新目录名" @keyup.enter="makeDirectory" /><button @click="makeDirectory">创建目录</button></div>
          <div class="library-list"><article v-for="item in images" :key="item.filename" class="library-row"><img :src="item.media_url" alt="" loading="lazy" /><div class="file-meta"><strong>{{ item.filename }}</strong><small>{{ Math.ceil(item.size / 1024) }} KB · {{ item.extension }}</small></div><button class="quiet" @click="rename(item)">重命名</button></article><div v-if="!images.length" class="empty-state compact"><h2>这个目录还没有图片</h2></div></div>
        </section>
        <section v-else-if="page === 'upload'" class="workspace narrow">
          <div class="section-head"><div><span class="eyebrow">IMAGE INGESTION</span><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF，批量操作会逐文件报告结果。</p></div></div>
          <div class="upload-panel"><label class="field">目标目录<select v-model="directory"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="item">{{ item }}</option></select></label><label class="drop-zone"><input type="file" multiple accept=".png,.jpg,.jpeg,.gif" @change="onFiles" /><span class="drop-title">选择图片文件</span><span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件` : '点击选择或拖入文件' }}</span></label><label class="switch"><input v-model="autoName" type="checkbox" /><span></span>尝试使用 VLM 自动命名</label><button class="primary wide" :disabled="busy || !files.length" @click="upload">{{ busy ? '上传中...' : '上传所选图片' }}</button></div>
          <div v-if="uploadResults.length" class="upload-results"><div v-for="item in uploadResults" :key="item.filename" class="upload-result" :class="{ fail: !item.ok }"><span>{{ item.ok ? '✓' : '!' }}</span><strong>{{ item.filename }}</strong><small>{{ item.ok ? item.saved_filename : item.error }}</small></div></div>
        </section>
        <section v-else class="workspace">
          <div class="section-head"><div><span class="eyebrow">IMAGE LABELING</span><h1>图片标注</h1><p>生成候选描述，确认后再修改文件名。</p></div></div>
          <div class="toolbar"><select v-model="labelDirectory" @change="loadLabelLibrary"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="item">{{ item }}</option></select><select v-model="labelFilename" @change="selectLabelImage(labelFilename)"><option value="">选择图片</option><option v-for="item in images" :key="item.filename" :value="item.filename">{{ item.filename }}</option></select><button @click="describe" :disabled="busy || !labelFilename">生成描述</button><button @click="startBatchLabel" :disabled="busy || !images.length">批量预生成</button></div>
          <div v-if="selectedImage" class="label-layout"><img class="label-preview" :src="selectedImage.media_url" alt="当前图片" /><div class="label-editor"><div class="label-nav"><button @click="navigateLabel(-1)">← 上一张</button><button @click="navigateLabel(1)">下一张 →</button></div><h2>{{ labelFilename }}</h2><div v-if="candidates.length" class="candidate-list"><button v-for="candidate in candidates" :key="candidate" @click="customName = candidate">{{ candidate }}</button></div><textarea v-model="customName" placeholder="输入或选择新的文件名"></textarea><button class="primary" :disabled="!customName.trim()" @click="submitLabel">提交重命名</button></div></div><div v-else class="empty-state compact"><h2>先从图片库选择一张图片</h2></div>
        </section>
      </main>
    </div>
  </div>
</template>
