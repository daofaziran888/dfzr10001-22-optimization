<template>
  <section class="page" data-module="task">
    <header class="page-head">
      <div>
        <h2>采集任务管理</h2>
        <p class="page-desc">任务启动时钉住解析模板的生效版本；读取输入格式、字段映射、类型转换、默认值、校验正则、输出格式均按该版本，模板升级不影响旧任务。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集任务</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">版本失败待办</span>
        <strong class="stat-value">{{ todos.length }}</strong>
      </article>
    </div>

    <!-- 版本失败待办：切换失败与并发冲突随工作台展示 -->
    <section v-if="todos.length" class="todo-panel">
      <h3>版本失败待办</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>模板编号</th><th>版本号</th><th>关联任务</th><th>类型</th><th>说明</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="String(todo.id)">
            <td>{{ todo.模板编号 }}</td>
            <td>v{{ todo.版本号 }}</td>
            <td>{{ todo.关联任务 || '—' }}</td>
            <td>{{ todo.待办类型 }}</td>
            <td>{{ todo.说明 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="dismissTodo(todo.id)">关闭待办</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前模板生效版本</th>
          <th>版本状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ row.当前模板生效版本 ?? '—' }}</td>
          <td>
            <span v-if="row.版本落后" class="lag-badge">沿用启动版本 v{{ row.模板版本 }}，模板已升级</span>
            <span v-else class="ok-badge">与生效版本一致</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openParse(row)">按钉住版本解析</button>
            <button class="link" type="button" @click="openDiagnostics(row)">历史诊断</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无采集任务数据，可先登记采集任务</td>
        </tr>
      </tbody>
    </table>

    <!-- 按任务钉住版本试跑：结果显式带出生效版本与逐字段解释 -->
    <div v-if="parsePanel" class="drawer-mask" @click.self="parsePanel = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>{{ parsePanel.task.任务编号 }} 解析试跑（钉住版本 v{{ parsePanel.task.模板版本 }}）</h3>
          <button class="btn ghost" type="button" @click="parsePanel = null">关闭</button>
        </header>
        <label class="filter-item">
          <span>输入数据（JSON，按该版本的输入格式与字段映射读取）</span>
          <textarea v-model="parsePanel.input" rows="8" class="json-input"></textarea>
        </label>
        <p class="page-desc">
          生效版本 v{{ parsePanel.task.模板版本 }}：默认值、类型转换、校验正则均取自该版本快照，
          模板后续新增的校验正则不会作用于本任务。
        </p>
        <button class="btn primary" type="button" @click="runParse">执行解析并回写诊断</button>
        <pre v-if="parsePanel.result" class="json-output">{{ formatJson(parsePanel.result) }}</pre>
      </div>
    </div>

    <!-- 历史诊断：按诊断当时的版本号原样展示，不随后续切换改写 -->
    <div v-if="diagnosticPanel" class="drawer-mask" @click.self="diagnosticPanel = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>{{ diagnosticPanel.taskCode }} 历史诊断（保持诊断时版本原样）</h3>
          <button class="btn ghost" type="button" @click="diagnosticPanel = null">关闭</button>
        </header>
        <p v-if="!diagnosticPanel.items.length" class="page-desc">暂无诊断记录</p>
        <article v-for="diag in diagnosticPanel.items" :key="String(diag.id)" class="diag-card">
          <header>
            <strong>诊断 #{{ diag.id }} · 版本 v{{ diag.模板版本 }} · {{ diag.诊断时间 }}</strong>
            <span :class="diag.通过 ? 'ok-badge' : 'lag-badge'">{{ diag.通过 ? '通过' : '未通过' }}</span>
          </header>
          <p v-if="diag.错误?.length" class="error-text">{{ diag.错误.join('；') }}</p>
          <table class="data-table">
            <thead>
              <tr>
                <th>目标字段</th><th>实际命中源字段</th><th>历史别名</th><th>套用默认值</th>
                <th>类型转换</th><th>校验正则</th><th>正则通过</th><th>输出值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="note in diag.字段解释" :key="note.目标字段">
                <td>{{ note.目标字段 }}</td>
                <td>{{ note.实际命中源字段 }}</td>
                <td>{{ note.使用历史别名 ? '是' : '否' }}</td>
                <td>{{ note.缺失并套用默认值 ? `是（${formatJson(note.默认值)}）` : '否' }}</td>
                <td>{{ note.类型转换 || '—' }}</td>
                <td>{{ note.校验正则 || '—' }}</td>
                <td>{{ note.校验正则 ? (note.正则通过 ? '通过' : '不通过') : '—' }}</td>
                <td>{{ formatJson(note.输出值) }}</td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | boolean>

const ENDPOINT = '/api/task'
const columns = ["任务编号", "任务名称", "关联源端", "解析模板编号", "模板版本"]
const actions = ["提交执行", "暂停任务", "恢复任务"]
const stats = [{"label": "采集任务总数", "value": 0}, {"label": "待执行任务", "value": 0}, {"label": "执行中任务", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const todos = ref<Array<Record<string, any>>>([])

const parsePanel = ref<{
  task: Row
  input: string
  result: Record<string, any> | null
} | null>(null)
const diagnosticPanel = ref<{ taskCode: string; items: Array<Record<string, any>> } | null>(null)

function formatJson(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集任务登记入口尚未接入审批流'
}

async function loadTodos() {
  try {
    const response = await request(`${ENDPOINT}/version-todos`)
    if (response.ok) {
      const payload = await response.json()
      todos.value = payload.items ?? []
    }
  } catch {
    // 待办拉取失败不阻塞主列表
  }
}

async function dismissTodo(todoId: number) {
  try {
    const response = await request(`/api/parser/todos/${todoId}/dismiss`, { method: 'POST' })
    if (response.ok) await loadTodos()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待办关闭失败'
  }
}

function openParse(row: Row) {
  parsePanel.value = { task: row, input: '{}', result: null }
}

async function runParse() {
  if (!parsePanel.value) return
  let data: Record<string, unknown>
  try {
    data = JSON.parse(parsePanel.value.input)
  } catch {
    errorMessage.value = '输入数据不是合法 JSON'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${parsePanel.value.task.id}/parse`, {
      method: 'POST',
      body: JSON.stringify({ 数据: data }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '解析失败')
    parsePanel.value.result = payload.entry
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析失败'
  }
}

async function openDiagnostics(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/diagnostics`)
    if (!response.ok) throw new Error('历史诊断读取失败')
    const payload = await response.json()
    diagnosticPanel.value = { taskCode: String(row.任务编号), items: payload.items ?? [] }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史诊断读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集任务动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadTodos()
})
</script>

<style scoped>
.todo-panel {
  background: #fff7ed;
  border: 1px solid #fdba74;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.todo-panel h3 { margin: 0 0 8px; font-size: 14px; }
.lag-badge {
  display: inline-block;
  padding: 2px 8px;
  font-size: 12px;
  border-radius: 10px;
  background: #fef3c7;
  color: #92400e;
}
.ok-badge {
  display: inline-block;
  padding: 2px 8px;
  font-size: 12px;
  border-radius: 10px;
  background: #dcfce7;
  color: #166534;
}
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.drawer {
  width: min(960px, 94vw);
  height: 100%;
  background: #fff;
  padding: 16px 20px;
  overflow: auto;
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.json-input {
  width: 100%;
  font-family: ui-monospace, monospace;
  font-size: 13px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.json-output {
  margin-top: 12px;
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 8px;
  font-size: 12px;
  overflow: auto;
}
.diag-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.diag-card header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
</style>
