<template>
  <section class="page" data-module="decision">
    <header class="page-head">
      <div>
        <h2>决策建议推送管理</h2>
        <p class="page-desc">维护决策建议的方案对比、收益评估与风险分析，为决策建议推送提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记决策建议</button>
        <button class="btn" type="button" @click="consumeBottlenecks">消费轨迹瓶颈数据集</button>
        <button class="btn" type="button" @click="exportRows">导出决策建议推送清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无决策建议推送数据，可先登记决策建议</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条决策建议推送记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/decision'
const columns = ["建议编号", "分析主题", "决策选项", "收益评估", "风险评估", "推荐方案", "生成时间", "建议状态"]
const actions = ["推送建议", "标记采纳", "驳回建议"]
const statuses = ["待审核", "已推送", "已采纳", "已驳回"]
const stats = [{"label": "决策建议总数", "value": 0}, {"label": "已采纳建议", "value": 0}, {"label": "已驳回建议", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '决策建议登记入口尚未接入审批流'
}

async function consumeBottlenecks() {
  errorMessage.value = ''
  try {
    const response = await request('/api/decision/trajectory-consume', { method: 'POST' })
    if (!response.ok) {
      throw new Error('瓶颈数据集消费失败')
    }
    const payload = await response.json()
    errorMessage.value = `${payload.message}；本次同步登记 ${payload.synced_to_decision ?? 0} 条，轨迹依据已随建议保存`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '跨模块消费失败'
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
      throw new Error('决策建议推送动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '决策建议推送操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('决策建议列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '决策建议推送列表读取失败'
  }
}

onMounted(reload)
</script>
