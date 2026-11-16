<template>
  <section class="page" data-module="trajectory">
    <header class="page-head">
      <div>
        <h2>供应链轨迹核算与瓶颈量化</h2>
        <p class="page-desc">
          对订单各环节开始/结束事件配对，按区间并集核算处理、等待、未完成与返工耗时；
          瓶颈按反事实区间并集量化延迟贡献，并行重叠自动抵扣，不直接相加环节耗时。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="resetSeed">重置样例数据</button>
        <button class="btn primary" type="button" @click="pushRecommendations">推送瓶颈建议</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="message" class="page-desc" :class="{ 'error-text': !messageOk }">{{ message }}</p>

    <!-- 订单轨迹 -->
    <div v-if="activeTab === 'orders'">
      <table class="data-table">
        <thead>
          <tr>
            <th>订单</th>
            <th>状态</th>
            <th>总历时</th>
            <th>处理(并集)</th>
            <th>等待</th>
            <th>未完成观测</th>
            <th>返工</th>
            <th>无效段</th>
            <th>对账</th>
            <th>问题</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="order in orders" :key="order.order_id">
            <tr>
              <td>{{ order.order_id }}</td>
              <td>
                <span class="badge" :class="order.is_closed ? 'ok' : 'warn'">
                  {{ order.is_closed ? '已闭合' : '未闭合' }}
                </span>
              </td>
              <td>{{ fmt(order.span_seconds) }}</td>
              <td>{{ fmt(order.processing_seconds) }}</td>
              <td>{{ fmt(order.waiting_seconds) }}</td>
              <td>{{ fmt(order.open_observed_seconds) }}</td>
              <td>{{ fmt(order.rework_seconds) }}</td>
              <td>{{ order.invalid_segment_count }}</td>
              <td>
                <span class="badge" :class="order.reconciliation.explainable ? 'ok' : 'warn'">
                  {{ order.reconciliation.explainable ? '可解释' : '不可强解' }}
                </span>
              </td>
              <td>
                <span v-for="issue in order.issues" :key="issue.code" class="badge issue" :title="issue.message">
                  {{ issue.code }}
                </span>
                <span v-if="!order.issues.length">—</span>
              </td>
              <td>
                <button class="link" type="button" @click="toggle(order.order_id)">
                  {{ expanded.has(order.order_id) ? '收起' : '展开轨迹' }}
                </button>
              </td>
            </tr>
            <tr v-if="expanded.has(order.order_id)">
              <td colspan="11" class="detail-cell">
                <div class="detail-grid">
                  <div>
                    <h4>作业段（含配对依据，关联尝试编号与区间边界）</h4>
                    <table class="data-table inner">
                      <thead>
                        <tr><th>环节</th><th>尝试</th><th>状态</th><th>开始</th><th>结束</th><th>耗时</th><th>配对/返工</th></tr>
                      </thead>
                      <tbody>
                        <tr v-for="seg in order.segments" :key="seg.segment_id">
                          <td>{{ seg.stage }}</td>
                          <td>#{{ seg.attempt }}</td>
                          <td><span class="badge" :class="segStatusClass(seg.status)">{{ segStatusLabel(seg.status) }}</span></td>
                          <td>{{ shortTime(seg.start_time) }}</td>
                          <td>{{ shortTime(seg.end_time) }}</td>
                          <td>
                            {{ seg.duration_seconds === null ? '—' : fmt(seg.duration_seconds) }}
                            <span v-if="seg.status === 'open'" class="muted">（观测 {{ fmt(seg.observed_seconds) }}）</span>
                          </td>
                          <td>
                            <div class="muted small">{{ seg.pairing.method }} · {{ seg.pairing.confidence }}</div>
                            <div v-if="seg.pairing.candidate_start_event_ids.length > 1" class="small warn-text">
                              候选：{{ seg.pairing.candidate_start_event_ids.join('、') }}
                            </div>
                            <div v-if="seg.rework.is_rework" class="small">返工（{{ seg.rework.kind }} 第{{ seg.rework.round }}轮）</div>
                            <div v-else-if="seg.rework.kind === 'parallel'" class="small">并行作业段</div>
                            <div v-for="msg in seg.issues" :key="msg" class="small warn-text">{{ msg }}</div>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <div class="side-col">
                    <h4>处理区间并集</h4>
                    <ul class="plain">
                      <li v-for="w in order.work_union" :key="w.start">
                        {{ shortTime(w.start) }} ~ {{ shortTime(w.end) }}（{{ fmt(w.seconds) }}）
                      </li>
                      <li v-if="!order.work_union.length" class="muted">无有效作业区间</li>
                    </ul>
                    <h4>等待区间</h4>
                    <ul class="plain">
                      <li v-for="gap in order.waiting_gaps" :key="gap.gap_id">
                        {{ shortTime(gap.start) }} ~ {{ shortTime(gap.end) }}（{{ fmt(gap.seconds) }}）
                      </li>
                      <li v-if="!order.waiting_gaps.length" class="muted">无</li>
                    </ul>
                    <h4>相互覆盖的作业段</h4>
                    <ul class="plain">
                      <li v-for="ov in order.overlaps" :key="ov.overlap_id" class="warn-text">
                        {{ ov.stages.join(' / ') }} 重叠 {{ fmt(ov.overlap_seconds) }}
                        <span v-if="ov.same_stage">（同环节并行）</span>
                      </li>
                      <li v-if="!order.overlaps.length" class="muted">无</li>
                    </ul>
                    <h4>无法确定的候选事件</h4>
                    <ul class="plain">
                      <li v-for="c in order.unpaired_candidates" :key="c.event_id" class="warn-text">
                        {{ c.event_id }}（{{ c.stage }} #{{ c.attempt }}）：{{ c.reason }}
                      </li>
                      <li v-if="!order.unpaired_candidates.length" class="muted">无</li>
                    </ul>
                    <h4>版本与对账</h4>
                    <p class="small muted">
                      时区：{{ order.timezone_offsets.join('、') || '—' }} ｜
                      成环环节：{{ order.parent_cycle_stages.join('、') || '无' }}
                    </p>
                    <p class="small">{{ order.reconciliation.formula }}</p>
                    <p v-if="order.reconciliation.explain_note" class="small warn-text">{{ order.reconciliation.explain_note }}</p>
                    <button class="btn small-btn" type="button" @click="retry(order.order_id)">重试配对（生成新版本）</button>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- 瓶颈数据集 -->
    <div v-if="activeTab === 'bottleneck'">
      <p class="page-desc">{{ bottleneck.method }}</p>
      <table class="data-table">
        <thead>
          <tr>
            <th>排名</th><th>环节</th><th>订单数</th><th>作业并集</th><th>边际贡献</th>
            <th>并行抵扣</th><th>返工</th><th>等待边界</th><th>效率得分</th><th>基准周期</th>
            <th>反事实可减少</th><th>瓶颈</th><th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="agg in bottleneck.stage_summary" :key="agg.stage">
            <tr>
              <td>{{ agg.bottleneck_rank }}</td>
              <td>{{ agg.stage }}</td>
              <td>{{ agg.order_count }}</td>
              <td>{{ fmt(agg.work_union_seconds) }}</td>
              <td>{{ fmt(agg.marginal_seconds) }}</td>
              <td>{{ fmt(agg.shared_parallel_seconds) }}</td>
              <td>{{ fmt(agg.rework_seconds) }}</td>
              <td>{{ fmt(agg.waiting_boundary_seconds) }}</td>
              <td>{{ agg.average_efficiency_score === null ? '无基准' : agg.average_efficiency_score }}</td>
              <td>{{ agg.benchmark_periods.join('、') || '—' }}</td>
              <td>
                <strong>{{ agg.delay_contribution_available ? fmt(agg.delay_contribution_seconds) : '待基准' }}</strong>
              </td>
              <td><span v-if="agg.is_bottleneck" class="badge issue">瓶颈</span><span v-else>—</span></td>
              <td><button class="link" type="button" @click="toggleB(agg.stage)">{{ bExpanded.has(agg.stage) ? '收起区间' : '贡献区间' }}</button></td>
            </tr>
            <tr v-if="bExpanded.has(agg.stage)">
              <td colspan="13" class="detail-cell">
                <table class="data-table inner">
                  <thead>
                    <tr><th>订单</th><th>类型</th><th>尝试</th><th>区间起</th><th>区间止</th><th>秒数</th><th>返工</th><th>基准说明</th></tr>
                  </thead>
                  <tbody>
                    <template v-for="row in rowsForStage(agg.stage)" :key="row.order_id">
                      <tr v-for="iv in row.contribution_intervals" :key="row.order_id + iv.start + iv.kind">
                        <td>{{ row.order_id }}</td>
                        <td>{{ kindLabel(iv.kind) }}</td>
                        <td>{{ iv.attempt === null ? '—' : '#' + iv.attempt }}</td>
                        <td>{{ shortTime(iv.start) }}</td>
                        <td>{{ shortTime(iv.end) }}</td>
                        <td>{{ fmt(iv.seconds) }}</td>
                        <td>{{ iv.rework ? '是' : '否' }}</td>
                        <td>{{ row.no_benchmark_reason || (row.benchmark ? row.benchmark.period : '—') }}</td>
                      </tr>
                      <tr v-if="!row.contribution_intervals.length">
                        <td>{{ row.order_id }}</td><td colspan="7" class="muted">该订单此环节仅有无效/未配对事件，不伪造贡献区间</td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- 基准与建议 -->
    <div v-if="activeTab === 'basis'">
      <h3>效率基准（注明适用环节与周期）</h3>
      <table class="data-table">
        <thead><tr><th>基准编号</th><th>环节</th><th>周期</th><th>标准耗时/次</th><th>适用起</th><th>适用止</th></tr></thead>
        <tbody>
          <tr v-for="b in benchmarks" :key="b.benchmark_id">
            <td>{{ b.benchmark_id }}</td><td>{{ b.stage }}</td><td>{{ b.period }}</td>
            <td>{{ fmt(b.standard_seconds) }}</td><td>{{ shortTime(b.applies_from) }}</td><td>{{ shortTime(b.applies_to) }}</td>
          </tr>
        </tbody>
      </table>

      <h3>轨迹驱动的决策建议</h3>
      <table class="data-table">
        <thead><tr><th>建议编号</th><th>环节</th><th>排名</th><th>决策选项</th><th>量化可减少</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <template v-for="rec in recommendations" :key="rec.recommendation_id">
            <tr>
              <td>{{ rec.recommendation_id }}</td>
              <td>{{ rec.stage }}</td>
              <td>{{ rec.bottleneck_rank }}</td>
              <td class="option-cell">{{ rec.decision_option }}</td>
              <td>{{ fmt(rec.quantified.delay_contribution_seconds) }} / {{ rec.quantified.affected_order_count }} 单</td>
              <td>{{ rec.status }}</td>
              <td><button class="link" type="button" @click="toggleR(rec.recommendation_id)">{{ rExpanded.has(rec.recommendation_id) ? '收起依据' : '轨迹依据' }}</button></td>
            </tr>
            <tr v-if="rExpanded.has(rec.recommendation_id)">
              <td colspan="7" class="detail-cell">
                <ul class="plain">
                  <li v-for="ev in rec.trajectory_evidence" :key="ev.order_id" class="evidence">
                    <strong>{{ ev.order_id }}</strong>
                    ：作业并集 {{ fmt(ev.work_union_seconds) }}，边际 {{ fmt(ev.marginal_seconds) }}，
                    并行抵扣 {{ fmt(ev.shared_parallel_seconds) }}，等待边界 {{ fmt(ev.waiting_boundary_seconds) }}，
                    返工 {{ fmt(ev.rework_seconds) }}，反事实贡献
                    <strong>{{ ev.delay_contribution_seconds === null ? '待基准' : fmt(ev.delay_contribution_seconds) }}</strong>
                    <span v-if="ev.no_benchmark_reason" class="warn-text">（{{ ev.no_benchmark_reason }}）</span>
                  </li>
                </ul>
              </td>
            </tr>
          </template>
          <tr v-if="!recommendations.length"><td colspan="7" class="empty-state">尚未推送，点击右上角「推送瓶颈建议」</td></tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span>数据源：事件配对轨迹版本（补报生成新版本，新旧配对不混算）</span>
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface Pairing { method: string; confidence: string; candidate_start_event_ids: string[]; basis: string }
interface Rework { is_rework: boolean; kind: string | null; round: number }
interface Segment {
  segment_id: string; stage: string; attempt: number; status: string
  start_time: string | null; end_time: string | null
  duration_seconds: number | null; observed_seconds?: number
  pairing: Pairing; rework: Rework; issues: string[]
}
interface Issue { code: string; level: string; message: string }
interface UnionPart { start: string; end: string; seconds: number }
interface OrderSummary {
  order_id: string; is_closed: boolean
  span_seconds: number | null; processing_seconds: number
  waiting_seconds: number; open_observed_seconds: number; rework_seconds: number
  invalid_segment_count: number; segment_count: number
  work_union: UnionPart[]; open_union: UnionPart[]
  waiting_gaps: Array<{ gap_id: string; start: string; end: string; seconds: number }>
  overlaps: Array<{ overlap_id: string; stages: string[]; overlap_seconds: number; same_stage: boolean }>
  segments: Segment[]; unpaired_candidates: Array<{ event_id: string; stage: string; attempt: number; reason: string }>
  issues: Issue[]; timezone_offsets: string[]; parent_cycle_stages: string[]
  reconciliation: { formula: string; explainable: boolean; explain_note: string | null }
}
interface StageSummary {
  stage: string; bottleneck_rank: number; is_bottleneck: boolean; order_count: number
  work_union_seconds: number; marginal_seconds: number; shared_parallel_seconds: number
  rework_seconds: number; waiting_boundary_seconds: number
  average_efficiency_score: number | null; benchmark_periods: string[]
  delay_contribution_seconds: number; delay_contribution_available: boolean
}
interface ContributionInterval {
  kind: string; segment_id: string | null; attempt: number | null
  start: string; end: string; seconds: number; rework: boolean
}
interface OrderStageRow {
  order_id: string; stage: string; completed_count: number; open_count: number
  no_benchmark_reason: string | null
  benchmark: { period: string; standard_seconds: number } | null
  contribution_intervals: ContributionInterval[]
}
interface BottleneckDataset {
  method: string; stage_summary: StageSummary[]; order_stage_rows: OrderStageRow[]
}
interface Benchmark {
  benchmark_id: string; stage: string; period: string; standard_seconds: number
  applies_from: string | null; applies_to: string | null
}
interface Recommendation {
  recommendation_id: string; stage: string; bottleneck_rank: number; is_bottleneck: boolean
  decision_option: string; status: string
  quantified: { delay_contribution_seconds: number; affected_order_count: number }
  trajectory_evidence: Array<{
    order_id: string; work_union_seconds: number; marginal_seconds: number
    shared_parallel_seconds: number; waiting_boundary_seconds: number; rework_seconds: number
    delay_contribution_seconds: number | null; no_benchmark_reason: string | null
  }>
}

const ENDPOINT = '/api/trajectory'
const tabs = [
  { key: 'orders', label: '订单轨迹' },
  { key: 'bottleneck', label: '瓶颈数据集' },
  { key: 'basis', label: '基准与建议' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('orders')
const orders = ref<OrderSummary[]>([])
const bottleneck = ref<BottleneckDataset>({ method: '', stage_summary: [], order_stage_rows: [] })
const benchmarks = ref<Benchmark[]>([])
const recommendations = ref<Recommendation[]>([])
const expanded = ref<Set<string>>(new Set())
const bExpanded = ref<Set<string>>(new Set())
const rExpanded = ref<Set<string>>(new Set())
const message = ref('')
const messageOk = ref(true)

const stats = ref([
  { label: '轨迹订单', value: 0 },
  { label: '已闭合', value: 0 },
  { label: '未闭合/异常', value: 0 },
  { label: '瓶颈环节', value: 0 },
])

function fmt(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds === 0) return '0s'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return [h ? `${h}h` : '', m ? `${m}m` : '', s ? `${s}s` : ''].filter(Boolean).join(' ') || '0s'
}
function shortTime(value: string | null | undefined): string {
  return value ? value.replace('T', ' ').replace('Z', ' UTC').slice(0, 21) : '—'
}
function segStatusLabel(status: string): string {
  return { completed: '已完成', open: '未完成', invalid_end_before_start: '结束早于开始' }[status] || status
}
function segStatusClass(status: string): string {
  return { completed: 'ok', open: 'warn', invalid_end_before_start: 'bad' }[status] || 'warn'
}
function kindLabel(kind: string): string {
  return { work: '作业', 'waiting-gap': '等待', 'open-observed': '未完成观测' }[kind] || kind
}
function rowsForStage(stage: string): OrderStageRow[] {
  return bottleneck.value.order_stage_rows.filter((r) => r.stage === stage)
}
function toggle(key: string) {
  const next = new Set(expanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expanded.value = next
}
function toggleB(key: string) {
  const next = new Set(bExpanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  bExpanded.value = next
}
function toggleR(key: string) {
  const next = new Set(rExpanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  rExpanded.value = next
}
function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function reload() {
  try {
    const [orderPayload, dataset, benchPayload, recoPayload] = await Promise.all([
      getJson<{ items: OrderSummary[] }>(`${ENDPOINT}/orders`),
      getJson<BottleneckDataset>(`${ENDPOINT}/bottlenecks`),
      getJson<{ items: Benchmark[] }>(`${ENDPOINT}/benchmarks`),
      getJson<{ items: Recommendation[] }>(`${ENDPOINT}/recommendations`),
    ])
    orders.value = orderPayload.items
    bottleneck.value = dataset
    benchmarks.value = benchPayload.items
    recommendations.value = recoPayload.items
    stats.value = [
      { label: '轨迹订单', value: orders.value.length },
      { label: '已闭合', value: orders.value.filter((o) => o.is_closed).length },
      { label: '未闭合/异常', value: orders.value.filter((o) => !o.is_closed).length },
      { label: '瓶颈环节', value: dataset.stage_summary.filter((s) => s.is_bottleneck).length },
    ]
  } catch (error) {
    notify(error instanceof Error ? error.message : '轨迹数据加载失败', false)
  }
}

async function resetSeed() {
  try {
    const response = await request(`${ENDPOINT}/seed/reset`, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message || '样例已重建')
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '重置失败', false)
  }
}

async function pushRecommendations() {
  try {
    const response = await request(`${ENDPOINT}/recommendations/push`, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message || '建议已推送')
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '推送失败', false)
  }
}

async function retry(orderId: string) {
  try {
    const response = await request(`${ENDPOINT}/orders/${orderId}/retry`, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message || '已重新配对')
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '重试失败', false)
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.tab-bar { display: flex; gap: 6px; margin-bottom: 12px; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 14px; cursor: pointer; }
.tab.active { background: var(--brand); color: #fff; border-color: var(--brand); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; background: #e5e7eb; }
.badge.ok { background: #dcfce7; color: #166534; }
.badge.warn { background: #fef9c3; color: #854d0e; }
.badge.bad { background: #fee2e2; color: #991b1b; }
.badge.issue { background: #ffedd5; color: #9a3412; margin-right: 4px; }
.detail-cell { background: #f8fafc; padding: 12px !important; }
.detail-grid { display: grid; grid-template-columns: 1.6fr 1fr; gap: 16px; }
.side-col h4, .detail-grid h4 { margin: 8px 0 6px; font-size: 13px; }
.data-table.inner { font-size: 12px; }
.plain { list-style: none; padding: 0; margin: 0 0 8px; font-size: 12px; }
.plain li { padding: 2px 0; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.small-btn { padding: 3px 10px; font-size: 12px; }
.warn-text { color: #9a3412; }
.option-cell { max-width: 360px; }
.evidence { padding: 4px 0; border-bottom: 1px dashed var(--border); }
h3 { font-size: 15px; margin: 18px 0 8px; }
</style>
