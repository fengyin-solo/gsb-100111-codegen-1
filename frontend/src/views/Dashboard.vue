<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>风险走廊</h2>
        <p class="page-desc">
          按里程展开的风险走廊：每段联动待处置病害等级、巡查逾期与桥梁限载提示；现场复核 → 等级裁定 → 封控发布单向推进，
          处置结论回写巡查台账、病害清单与本看板，三者读取同一裁定版本。
        </p>
      </div>
      <div class="page-actions">
        <span class="replay-state" :class="{ offline: !replayOnline }">{{ replayState }}</span>
        <button class="btn" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <form v-if="actionTarget" class="action-panel" @submit.prevent="submitAction">
      <strong>{{ actionTarget.action }} · {{ actionTarget.segment.走廊段编号 }}（{{ actionTarget.segment.桩号区间 }}）</strong>
      <p class="panel-hint">{{ actionHint }}</p>
      <label v-if="actionTarget.action === '现场复核'" class="filter-item">
        <span>复核等级</span>
        <select v-model="form.复核等级">
          <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
        </select>
      </label>
      <label v-if="actionTarget.action === '现场复核'" class="filter-item">
        <span>复核人</span>
        <input v-model="form.复核人" placeholder="现场复核人" />
      </label>
      <label v-if="actionTarget.action === '派单处置'" class="filter-item">
        <span>承建单位</span>
        <input v-model="form.承建单位" placeholder="默认市养护工程中心" />
      </label>
      <span class="panel-key">上报编号：{{ reportKey }}（重试复用同一编号，重复上报不累加）</span>
      <div class="panel-actions">
        <button class="btn primary" type="submit" :disabled="submitting">{{ submitting ? '提交中…' : '确认提交' }}</button>
        <button class="btn ghost" type="button" @click="closeAction">取消</button>
      </div>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>桩号区间</th>
          <th>路段</th>
          <th>待处置病害</th>
          <th>巡查逾期</th>
          <th>桥梁限载</th>
          <th>上报/复核/裁定</th>
          <th>阶段</th>
          <th>裁定版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in segments" :key="row.id">
          <td>{{ row.桩号区间 }}</td>
          <td>{{ row.路段名称 }}</td>
          <td>
            <span class="badge" :class="levelClass(row.待处置病害等级)">{{ row.待处置病害等级 }}</span>
            {{ row.待处置病害数 }} 处
          </td>
          <td>
            <span class="badge" :class="row.巡查逾期 ? 'danger' : 'ok'">
              {{ row.巡查逾期 ? `逾期 ${row.巡查逾期天数} 天` : '未逾期' }}
            </span>
          </td>
          <td><span class="badge" :class="row.限载 ? 'warn' : 'muted'">{{ row.桥梁限载提示 }}</span></td>
          <td>
            {{ row.上报等级 }} / {{ row.复核等级 }} / {{ row.裁定等级 }}
            <span v-if="row.等级冲突" class="badge warn" title="等级冲突时以现场复核和限载裁定为准">冲突</span>
          </td>
          <td>{{ row.阶段 }}</td>
          <td>V{{ row.裁定版本 }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              :class="{ muted: !actionAllowed(row, action) }"
              type="button"
              @click="openAction(row, action)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!segments.length">
          <td colspan="9" class="empty-state">暂无风险走廊数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">
      处置结论同步记录
      <span class="section-sub">事件游标 {{ cursor }}，重放连接断开后按游标继续</span>
    </h3>
    <table class="data-table">
      <thead>
        <tr><th>游标</th><th>类型</th><th>走廊段</th><th>裁定版本</th><th>结论</th><th>同步目标</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="event in events" :key="event.cursor">
          <td>{{ event.cursor }}</td>
          <td>{{ event.类型 }}</td>
          <td>{{ event.走廊段编号 }}</td>
          <td>V{{ event.裁定版本 }}</td>
          <td>{{ event.结论 }}</td>
          <td>{{ event.同步目标 }}</td>
          <td>{{ event.时间 }}</td>
        </tr>
        <tr v-if="!events.length">
          <td colspan="7" class="empty-state">暂无处置事件，从风险桩号发起复核、裁定、封控或派单后在此重放</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">
      历史封控记录
      <span class="section-sub">封控等级按发布时的原等级保留，后续裁定不回写</span>
    </h3>
    <table class="data-table">
      <thead>
        <tr><th>封控编号</th><th>走廊段</th><th>桩号区间</th><th>封控等级</th><th>裁定版本</th><th>发布时间</th><th>状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in closures" :key="row.封控编号">
          <td>{{ row.封控编号 }}</td>
          <td>{{ row.走廊段编号 }}</td>
          <td>{{ row.桩号区间 }}</td>
          <td><span class="badge" :class="levelClass(row.封控等级)">{{ row.封控等级 }}</span></td>
          <td>V{{ row.裁定版本 }}</td>
          <td>{{ row.发布时间 }}</td>
          <td>{{ row.状态 }}</td>
        </tr>
        <tr v-if="!closures.length">
          <td colspan="7" class="empty-state">暂无封控记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ segments.length }} 个走廊段</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Segment = {
  id: number
  走廊段编号: string
  路段名称: string
  起点桩号: string
  终点桩号: string
  桩号区间: string
  上报等级: string
  复核等级: string
  裁定等级: string
  等级来源: string
  裁定版本: number
  阶段: string
  下一动作: string
  待处置病害数: number
  待处置病害等级: string
  巡查到期日: string
  巡查逾期: boolean
  巡查逾期天数: number
  限载: boolean
  桥梁限载提示: string
  等级冲突: boolean
}

type CorridorEvent = {
  cursor: number
  类型: string
  走廊段编号: string
  桩号区间: string
  裁定版本: number
  结论: string
  上报编号: string
  时间: string
  同步目标: string
}

type Closure = {
  封控编号: string
  走廊段编号: string
  桩号区间: string
  封控等级: string
  裁定版本: number
  发布时间: string
  状态: string
}

const ENDPOINT = '/api/risk_corridor'
const actions = ['现场复核', '等级裁定', '封控发布', '派单处置']
const levels = ['一般', '较大', '重大']

const segments = ref<Segment[]>([])
const cards = ref<{ label: string; value: number }[]>([])
const events = ref<CorridorEvent[]>([])
const closures = ref<Closure[]>([])
const cursor = ref(0)
const replayOnline = ref(true)
const replayState = ref('事件重放连接中…')
const errorMessage = ref('')
const noticeMessage = ref('')
const submitting = ref(false)
const actionTarget = ref<{ segment: Segment; action: string } | null>(null)
const reportKey = ref('')
const form = ref({ 复核等级: '一般', 复核人: '', 承建单位: '' })

let timer: ReturnType<typeof setInterval> | undefined

const ACTION_HINTS: Record<string, string> = {
  现场复核: '登记现场复核等级；与上报等级冲突时，裁定以现场复核为准。',
  等级裁定: '裁定等级以现场复核为准；桥梁限载的走廊段按限载裁定抬级，裁定版本 +1 并同步巡查台账与病害清单。',
  封控发布: '按当前裁定等级发布封控，结论同步巡查台账、病害清单与总览看板；历史封控记录按原等级保留。',
  派单处置: '生成养护工程待办并重算，结论同步巡查台账、病害清单与总览看板。',
}

const actionHint = computed(() => (actionTarget.value ? ACTION_HINTS[actionTarget.value.action] : ''))

function levelClass(level: string) {
  if (level === '重大') return 'danger'
  if (level === '较大') return 'warn'
  if (level === '一般') return 'info'
  return 'muted'
}

function actionAllowed(row: Segment, action: string) {
  if (action === '现场复核') return row.阶段 === '待复核'
  if (action === '等级裁定') return row.阶段 === '已复核'
  return row.阶段 === '已裁定'
}

function newReportKey() {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `RK-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
}

function openAction(row: Segment, action: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  actionTarget.value = { segment: row, action }
  reportKey.value = newReportKey()
  form.value = { 复核等级: row.上报等级 === '—' ? '一般' : row.上报等级, 复核人: '', 承建单位: '' }
}

function closeAction() {
  actionTarget.value = null
}

async function submitAction() {
  const target = actionTarget.value
  if (!target || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${target.segment.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          action: target.action,
          上报编号: reportKey.value,
          复核等级: form.value.复核等级,
          复核人: form.value.复核人,
          承建单位: form.value.承建单位,
        },
      }),
    })
    const payload = (await response.json()) as { ok: boolean; message: string }
    if (!payload.ok) {
      // 阶段校验、跳级拦截等业务原因：保留面板与上报编号，修正后可原样重试
      errorMessage.value = payload.message
      return
    }
    noticeMessage.value = payload.message
    actionTarget.value = null
    await Promise.all([reload(), pollEvents()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '风险走廊操作失败'
  } finally {
    submitting.value = false
  }
}

async function reload() {
  try {
    const [segmentPage, summary, closurePage] = await Promise.all([
      fetchJson<{ items: Segment[]; total: number }>(`${ENDPOINT}?size=200`),
      fetchJson<{ cards: { label: string; value: number }[] }>(`${ENDPOINT}/summary`),
      fetchJson<{ items: Closure[] }>(`${ENDPOINT}/closures`),
    ])
    segments.value = segmentPage.items
    cards.value = summary.cards
    closures.value = closurePage.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '风险走廊数据读取失败'
  }
}

async function pollEvents() {
  try {
    const payload = await fetchJson<{ events: CorridorEvent[]; latest: number }>(
      `${ENDPOINT}/events?after=${cursor.value}&limit=100`,
    )
    if (payload.events.length) {
      events.value = [...payload.events].reverse().concat(events.value)
    }
    cursor.value = payload.latest
    replayOnline.value = true
    replayState.value = `事件重放已连接 · 游标 ${cursor.value}`
  } catch {
    // 连接断开不清空游标，下一轮轮询从同一游标继续重放
    replayOnline.value = false
    replayState.value = `连接断开，按游标 ${cursor.value} 等待重放`
  }
}

onMounted(async () => {
  await Promise.all([reload(), pollEvents()])
  timer = setInterval(() => void pollEvents(), 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
