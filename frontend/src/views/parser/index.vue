<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>数据解析模板管理</h2>
        <p class="page-desc">维护采集结果的字段映射、类型转换与校验规则模板；每个模板按不可变版本归档，任务只读取启动时钉住的版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记解析模板</button>
        <button class="btn" type="button" @click="exportRows">导出数据解析模板清单</button>
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

    <!-- 版本失败待办：切换失败/并发冲突在此可见，可逐条关闭 -->
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row[column]) }}</td>
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
            <button class="link" type="button" @click="openVersions(row)">版本档案</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据解析模板数据，可先登记解析模板</td>
        </tr>
      </tbody>
    </table>

    <!-- 版本档案抽屉：展示各版本状态与规则，支持发起切换 -->
    <div v-if="versionPanel" class="drawer-mask" @click.self="versionPanel = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>{{ versionPanel.code }} 版本档案（生效版本 v{{ versionPanel.effective }}）</h3>
          <button class="btn ghost" type="button" @click="versionPanel = null">关闭</button>
        </header>
        <table class="data-table">
          <thead>
            <tr>
              <th>版本</th><th>状态</th><th>默认值</th><th>校验正则</th><th>字段改名</th><th>失败原因</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ver in versionPanel.items" :key="String(ver.id)">
              <td>v{{ ver.版本号 }}<span v-if="ver.是否生效" class="badge">生效中</span></td>
              <td>{{ ver.状态 }}</td>
              <td>{{ formatCell(ver.默认值) }}</td>
              <td>{{ formatCell(ver.校验正则) }}</td>
              <td>
                <span v-if="!ver.字段改名记录?.length">—</span>
                <span v-else>{{ ver.字段改名记录.map((r: any) => `${r.源字段}→${r.新源字段}`).join('，') }}</span>
              </td>
              <td>{{ ver.失败原因 || '—' }}</td>
              <td class="row-actions">
                <button
                  v-if="!ver.是否生效"
                  class="link"
                  type="button"
                  :disabled="ver.状态 === '切换失败'"
                  :title="ver.状态 === '切换失败' ? '切换失败的版本需修正后重新起草' : ''"
                  @click="switchVersion(versionPanel.code, ver.版本号)"
                >
                  切换到此版本
                </button>
                <span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据解析模板记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | Record<string, unknown>>

const ENDPOINT = '/api/parser'
const columns = ["模板编号", "输入格式", "字段映射", "类型转换", "默认值", "校验正则", "输出格式", "模板版本"]
const actions = ["提交校验", "启用配置", "停用配置"]
const stats = [{"label": "解析模板总数", "value": 0}, {"label": "待校验解析模板", "value": 0}, {"label": "已启用解析模板", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const todos = ref<Array<Record<string, any>>>([])
const versionPanel = ref<{ code: string; effective: number | null; items: Array<Record<string, any>> } | null>(null)

function formatCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '解析模板登记入口尚未接入审批流'
}

async function loadTodos() {
  try {
    const response = await request(`${ENDPOINT}/todos`)
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
    const response = await request(`${ENDPOINT}/todos/${todoId}/dismiss`, { method: 'POST' })
    if (response.ok) await loadTodos()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待办关闭失败'
  }
}

async function openVersions(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/code/${encodeURIComponent(String(row.模板编号))}/versions`)
    if (!response.ok) throw new Error('版本档案读取失败')
    const payload = await response.json()
    versionPanel.value = {
      code: payload.模板编号,
      effective: payload.生效版本号,
      items: payload.items ?? [],
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本档案读取失败'
  }
}

async function switchVersion(code: string, version: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/code/${encodeURIComponent(code)}/switch`, {
      method: 'POST',
      body: JSON.stringify({ 版本号: version }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '版本切换失败')
    await Promise.all([reload(), loadTodos()])
    if (versionPanel.value) await openVersions({ 模板编号: code } as Row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本切换失败'
    await Promise.all([reload(), loadTodos()])
    if (versionPanel.value) await openVersions({ 模板编号: code } as Row)
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
      throw new Error('数据解析模板动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据解析模板操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据解析模板列表读取失败'
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
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.drawer {
  width: min(920px, 92vw);
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
.badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 10px;
  background: #dcfce7;
  color: #166534;
}
</style>
