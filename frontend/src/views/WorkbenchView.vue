<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button
          v-if="meta.key !== 'metric_archive'"
          class="btn primary"
          data-testid="open-create"
          type="button"
          @click="openCreate"
        >
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'metric_definition'"
              class="link"
              type="button"
              :data-testid="`archive-${row.id}`"
              @click="openArchive(row)"
            >
              发布指标归档
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showArchive" class="modal-backdrop" data-testid="archive-modal">
      <form class="modal" @submit.prevent="submitArchive">
        <h3>发布指标归档</h3>
        <p class="page-desc">
          {{ archiveRow?.指标名称 }} 将被登记为指标下线记录；引用它的模板会在下方列出，
          数据服务随即停止接受新计算。
        </p>
        <label class="create-field">
          <span>下线原因</span>
          <input v-model="archiveReason" data-testid="archive-reason" required />
        </label>
        <div v-if="referenceSummary" class="status-legend">
          <span class="legend-item">查询模板：{{ referenceSummary.query_templates.length }}</span>
          <span class="legend-item">报告模板：{{ referenceSummary.report_templates.length }}</span>
          <span class="legend-item">数据服务：{{ referenceSummary.data_services.length }}</span>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="showArchive = false">取消</button>
          <button class="btn primary" data-testid="submit-archive" type="submit" :disabled="archiving">
            {{ archiving ? '归档中…' : '确认归档' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  archiveMetric,
  createEntry,
  downloadEntries,
  listEntries,
  metricReferences,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showArchive = ref(false)
const archiving = ref(false)
const archiveRow = ref<EntryRow | null>(null)
const archiveReason = ref('')
type ReferenceSummary = {
  query_templates: unknown[]
  report_templates: unknown[]
  data_services: unknown[]
}
const referenceSummary = ref<ReferenceSummary | null>(null)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function openArchive(row: EntryRow) {
  errorMessage.value = ''
  archiveRow.value = row
  archiveReason.value = '运营发起指标下线'
  referenceSummary.value = null
  showArchive.value = true
  try {
    const refs = await metricReferences(String(row['指标名称'] ?? ''))
    referenceSummary.value = refs as unknown as ReferenceSummary
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '引用面查询失败'
  }
}

async function submitArchive() {
  if (!archiveRow.value) return
  archiving.value = true
  errorMessage.value = ''
  try {
    const result = await archiveMetric(Number(archiveRow.value.id), archiveReason.value)
    if (!result.ok) throw new Error(result.message)
    showArchive.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档失败'
  } finally {
    archiving.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
