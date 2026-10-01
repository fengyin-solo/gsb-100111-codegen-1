<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>道桥养护总览 · 风险走廊</h2>
        <p class="page-desc">
          按里程展开的风险走廊：每段同步显示待处置病害等级、巡查逾期与桥梁限载提示，
          按「现场复核 → 等级裁定 → 封控发布」单向推进，未复核不得跳级封控。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新看板</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>桩号区间</th>
          <th>路段</th>
          <th>待处置病害</th>
          <th>巡查逾期</th>
          <th>桥梁限载</th>
          <th>推进阶段</th>
          <th>裁定版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="seg in segments" :key="seg.id">
          <td>{{ seg.起点桩号 }} ~ {{ seg.终点桩号 }}</td>
          <td>{{ seg.路段名称 }}</td>
          <td>
            <span class="badge" :class="gradeClass(seg.有效等级)">{{ seg.有效等级 }}</span>
            <span class="muted"> × {{ seg.待处置病害数 }} 起</span>
          </td>
          <td>
            <span v-if="seg.巡查逾期天数 > 0" class="badge danger">逾期 {{ seg.巡查逾期天数 }} 天</span>
            <span v-else class="muted">—</span>
          </td>
          <td>
            <span v-if="seg.桥梁限载" class="badge warn">{{ seg.桥梁限载 }}</span>
            <span v-else class="muted">—</span>
          </td>
          <td>
            <div class="stage-flow">
              <span
                v-for="(stage, index) in STAGES"
                :key="stage"
                class="stage-node"
                :class="{ active: index <= seg.阶段序号 }"
              >
                {{ stage }}
              </span>
            </div>
            <div v-if="seg.处置结论" class="muted conclusion">结论：{{ seg.处置结论 }}</div>
            <div v-else-if="seg.dispatch" class="muted conclusion">已派单：{{ seg.dispatch.处置单位 }}</div>
          </td>
          <td>v{{ seg.ruling_version }}</td>
          <td class="row-actions">
            <button
              v-for="action in seg.可执行动作"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, seg)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!segments.length">
          <td colspan="8" class="empty-state">暂无风险走廊数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ segments.length }} 个走廊段 · 事件游标 {{ cursor }}</span>
      <span v-if="notice" class="notice-text">{{ notice }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

const STAGES = ['待复核', '已复核', '已裁定', '已封控']
const GRADES = ['轻微', '一般', '较重', '严重', '危急']

interface CorridorSegment {
  id: number
  路段名称: string
  起点桩号: string
  终点桩号: string
  有效等级: string
  待处置病害数: number
  巡查逾期天数: number
  桥梁限载: string | null
  阶段序号: number
  ruling_version: number
  可执行动作: string[]
  处置结论: string | null
  dispatch: { 处置单位: string; 派单时间: string } | null
}

interface Overview {
  cards: { label: string; value: number }[]
  cursor: number
}

const cards = ref<Overview['cards']>([])
const segments = ref<CorridorSegment[]>([])
const cursor = ref(0)
const notice = ref('')
const errorMessage = ref('')

function gradeClass(grade: string) {
  if (grade === '危急' || grade === '严重') return 'danger'
  if (grade === '较重') return 'warn'
  if (grade === '一般') return 'info'
  return 'ok'
}

function promptValues(action: string): Record<string, string> | null {
  if (action === '现场复核') {
    const grade = window.prompt(`请输入复核等级（${GRADES.join('/')}）`, '较重')
    if (!grade) return null
    const reviewer = window.prompt('请输入复核人', '值班员') || '值班员'
    return { 复核等级: grade.trim(), 复核人: reviewer.trim() }
  }
  if (action === '等级裁定') {
    const grade = window.prompt(`请输入裁定等级（${GRADES.join('/')}）`, '严重')
    if (!grade) return null
    const load = window.prompt('桥梁限载裁定（可留空）', '') || ''
    return { 裁定等级: grade.trim(), 桥梁限载: load.trim() }
  }
  if (action === '封控发布') {
    const measure = window.prompt('请输入封控措施', '半幅封闭，限速40km/h')
    if (!measure) return null
    return { 封控措施: measure.trim() }
  }
  if (action === '派单') {
    const unit = window.prompt('请输入处置单位', '养护一队')
    if (!unit) return null
    return { 处置单位: unit.trim() }
  }
  if (action === '处置结论') {
    const conclusion = window.prompt('请输入处置结论', '病害已处置，复核通过')
    if (!conclusion) return null
    return { 结论: conclusion.trim() }
  }
  return {}
}

async function runAction(action: string, seg: CorridorSegment) {
  const values = promptValues(action)
  if (values === null) return
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request(`/api/risk-corridor/segments/${seg.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, ...values } }),
    })
    const result = await response.json()
    if (!result.ok) {
      errorMessage.value = result.message || '动作未生效'
      return
    }
    notice.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '风险走廊操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const [overviewPayload, segmentPayload] = await Promise.all([
      fetchJson<Overview>('/api/risk-corridor/overview'),
      fetchJson<{ items: CorridorSegment[] }>('/api/risk-corridor/segments'),
    ])
    cards.value = overviewPayload.cards
    cursor.value = overviewPayload.cursor
    segments.value = segmentPayload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '风险走廊数据读取失败'
  }
}

onMounted(reload)
</script>
