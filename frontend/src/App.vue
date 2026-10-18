<template>
  <main class="app-shell app-shell-developer">
    <header class="topbar">
      <a class="brand" href="#" aria-label="DevTrace 首页"><span class="brand-mark">D</span><span class="brand-name">DevTrace</span></a>
      <div class="topbar-tools"><span class="environment-pill"><i :class="developerHealthOk ? 'online' : 'offline'"></i>模拟平台 · {{ developerHealthOk ? '已连接' : '未连接' }}</span><a class="docs-link" :href="developerDocsUrl" target="_blank" rel="noreferrer">API 文档</a><button v-if="token" class="quiet-button" @click="logout">退出登录</button></div>
    </header>
    <section v-if="!token" class="page page-developer">
      <div class="workspace-card" style="max-width: 440px; margin: 4rem auto; padding: 2rem">
        <h1>登录 DevTrace</h1><p>使用管理员在本地配置的演示账号。</p>
        <form class="composer" @submit.prevent="signIn">
          <input v-model="username" autocomplete="username" placeholder="用户名" required>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="密码" required>
          <p v-if="loginError" role="alert">{{ loginError }}</p>
          <button type="submit" :disabled="loginBusy">{{ loginBusy ? '登录中' : '登录' }}</button>
        </form>
      </div>
    </section>
    <section v-else class="page page-developer">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Developer support lab</span>
          <h1>API 接入诊断助手</h1>
          <p>查看 Agent 如何制定计划、查询模拟平台记录，并依据观察结果完成排查。</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.developerConversationId || '新会话' }}</span>
          <button class="quiet-button" :disabled="developerBusy" @click="clearDeveloperConversation">清空</button>
        </div>
      </div>
      <div class="developer-layout">
        <section class="developer-chat workspace-card">
          <div class="stage-bar"><span>Python Agent · 本地模拟平台</span><span>{{ developerMessages.length }} 条消息</span></div>
          <div class="developer-messages" ref="developerMessageList">
            <article v-for="item in developerMessages" :key="item.id" :class="['developer-message', item.role]">
              <small>{{ item.role === 'user' ? '你' : '开发者助手' }}</small>
              <p>{{ item.content }}</p>
              <small v-if="item.runId">Run ID: {{ item.runId }}</small>
            </article>
            <div v-if="!developerMessages.length" class="developer-empty">
              <h2>从一条可复现的问题开始</h2>
              <p>案例数据均来自本地模拟平台。可在下一轮补充标识符，观察计划恢复。</p>
              <div class="starter-prompts">
                <button @click="developerDraft = '请求 req_auth_01 返回 401，帮我排查'">401 鉴权</button>
                <button @click="developerDraft = '请求 req_quota_01 遇到 429，如何处理'">429 限流</button>
                <button @click="developerDraft = 'Webhook 投递 delivery_01 失败，请排查'">Webhook 投递</button>
                <button @click="developerDraft = 'Webhook delivery_combo_01 失败，帮我排查'">关联故障</button>
              </div>
            </div>
          </div>
          <form class="composer" @submit.prevent="sendDeveloperMessage">
            <textarea v-model="developerDraft" rows="3" placeholder="描述 API 问题，或补充 request_id / delivery_id…"
              @keydown.meta.enter.prevent="sendDeveloperMessage" @keydown.ctrl.enter.prevent="sendDeveloperMessage"></textarea>
            <div class="composer-bottom">
              <span>支持跨轮补充信息；创建工单需明确提出</span>
              <button v-if="developerBusy" type="button" @click="cancelRun">停止本次请求</button>
              <button v-else type="submit" :disabled="!developerDraft.trim()">发送</button>
              <button v-if="retryRequest && !developerBusy" type="button" @click="retryLastRequest">重试上次请求</button>
            </div>
          </form>
        </section>
        <aside class="developer-inspector">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">History</span><h2>最近运行</h2></div><button class="quiet-button" @click="refreshRuns">刷新</button></div>
            <p v-if="historyError" role="alert">{{ historyError }}</p>
            <p v-if="!runs.length" class="side-empty">运行记录保存 24 小时。</p>
            <ol v-else class="developer-timeline">
              <li v-for="run in runs" :key="run.run_id"><button class="quiet-button" :disabled="developerBusy" @click="openRun(run.run_id)">{{ run.goal }} · {{ run.status }}</button></li>
            </ol>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Plan</span><h2>执行计划</h2></div><strong>{{ developerLast?.plan?.status || '待开始' }}</strong></div>
            <p v-if="developerLast?.plan?.goal" class="developer-goal">{{ developerLast.plan.goal }}</p>
            <p v-if="developerLast?.decision_mode" class="developer-goal">决策来源：{{ developerLast.decision_mode === 'model' ? '配置模型' : developerLast.decision_mode === 'rules_fallback' ? '规则降级（模型决策未完成）' : '本地规则模型' }}</p>
            <p v-if="developerLast?.plan?.proposed_tools?.length" class="developer-goal">计划提议：{{ developerLast.plan.proposed_tools.join(' → ') }}</p>
            <p v-if="developerLast?.plan?.approved_tools?.length" class="developer-goal">批准计划：{{ developerLast.plan.approved_tools.join(' → ') }}</p>
            <p v-for="(adjustment, index) in developerLast?.plan?.adjustments || []" :key="`adjustment-${index}`" class="developer-goal">约束调整：{{ adjustment }}</p>
            <ol v-if="developerLast?.plan?.steps?.length" class="developer-steps">
              <li v-for="step in developerLast.plan.steps" :key="step.id" :class="`step-${step.status}`">
                <span>{{ step.id }}</span><div><strong>{{ step.title }}</strong><small>所需证据：{{ step.required_evidence }}</small><small>{{ step.tool || '等待用户补充' }} · {{ step.status }}</small></div>
              </li>
            </ol>
            <p v-else class="side-empty">发送问题后，计划会显示在这里。</p>
            <p v-if="developerLast?.plan?.replans" class="developer-goal">已调整计划 {{ developerLast.plan.replans }} 次</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Timeline</span><h2>执行时间线</h2></div></div>
            <ol v-if="developerLast?.timeline?.length" class="developer-timeline">
              <li v-for="(event, index) in developerLast.timeline" :key="index" :class="`event-${event.type}`">
                <small>{{ event.type }}</small><span>{{ event.text }}</span>
              </li>
            </ol>
            <p v-else class="side-empty">这里会按顺序记录初始计划、行动、观察和状态变化。</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Action / observation</span><h2>工具与观察</h2></div></div>
            <div v-if="developerLast?.actions?.length" class="developer-actions">
              <details v-for="(action, index) in developerLast.actions" :key="index" open>
                <summary><strong>{{ action.tool }}</strong><span>{{ action.success ? '成功' : '失败' }}</span></summary>
                <p>{{ action.summary }}</p><small>来源：{{ action.source }}</small>
                <small v-if="action.tainted" class="action-tainted">疑似指令内容，已按数据处理</small>
              </details>
            </div>
            <p v-else class="side-empty">这里展示可验证的行动与观察摘要，不展示模型内部推理。</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Evidence</span><h2>证据引用</h2></div><strong v-if="developerLast?.diagnosis?.status" class="diagnosis-status">{{ diagnosisStatusLabel(developerLast.diagnosis.status) }}</strong></div>
            <p v-if="developerLast?.diagnosis?.summary" class="diagnosis-summary">{{ developerLast.diagnosis.summary }}</p>
            <div v-for="item in developerLast?.diagnosis?.hypotheses || []" :key="item.code" class="hypothesis">
              <p class="hypothesis-head">
                <strong>{{ item.description }}</strong>
                <span :class="['hypothesis-status', `hypothesis-status-${item.status}`]">{{ hypothesisStatusLabel(item.status) }}</span>
                <span v-if="item.confidence" class="hypothesis-confidence">置信度 {{ confidenceLabel(item.confidence) }}</span>
              </p>
              <p v-if="item.evidence?.length" class="hypothesis-line">依据：{{ item.evidence.join('、') }}</p>
              <p v-if="item.contradicted_by?.length" class="hypothesis-line hypothesis-contradicted">反向证据：{{ item.contradicted_by.join('、') }}</p>
              <p v-if="item.next_evidence" class="hypothesis-line">下一步证据：{{ item.next_evidence }}</p>
              <p v-if="item.remediation" class="hypothesis-line">建议处理：{{ item.remediation }}</p>
            </div>
            <div v-if="developerLast?.diagnosis?.conflicts?.length" class="diagnosis-conflict">
              <p class="section-label">证据冲突</p>
              <div v-for="(conflict, index) in developerLast.diagnosis.conflicts" :key="`conflict-${index}`" class="conflict-item">
                <p>{{ conflict.explanation }}</p>
                <small v-if="conflict.sources?.length">{{ conflict.sources.join(' ↔ ') }}</small>
              </div>
            </div>
            <div v-if="developerLast?.diagnosis?.linkages?.length" class="diagnosis-linkage">
              <p class="section-label">关联分析</p>
              <div v-for="(linkage, index) in developerLast.diagnosis.linkages" :key="`linkage-${index}`" class="linkage-item">
                <p><strong>{{ linkageAssessmentLabel(linkage.assessment) }}</strong><template v-if="linkage.explanation"> · {{ linkage.explanation }}</template></p>
                <small>投递 {{ linkage.delivery_id || '—' }} ↔ 关联请求 {{ linkage.related_request_id || '—' }}</small>
              </div>
            </div>
            <ul v-if="developerLast?.evidence?.length" class="developer-evidence">
              <li v-for="(source, index) in developerLast.evidence" :key="`${source}-${index}`">{{ source }}</li>
            </ul>
            <p v-else class="side-empty">成功核验的文档和模拟记录会列在这里。</p>
          </section>
          <section v-if="developerLast?.security" class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Security</span><h2>安全边界</h2></div><strong v-if="developerLast.security.injection_suspected" class="security-mark">已标记</strong><strong v-else class="security-clear">正常</strong></div>
            <p v-if="developerLast.security.injection_suspected" class="security-alert">检测到疑似指令内容：相关文本已按不可信数据处理，未作为指令执行。</p>
            <p v-else class="side-empty">未发现疑似指令或越权诱导。</p>
            <ul v-if="developerLast.security.flags?.length" class="security-flags">
              <li v-for="(flag, index) in developerLast.security.flags" :key="`flag-${index}`">
                <strong>{{ securityKindLabel(flag.kind) }}</strong>
                <small>{{ flag.origin || '未知来源' }}<template v-if="flag.source"> · {{ flag.source }}</template></small>
                <p v-if="flag.excerpt" class="security-excerpt">{{ flag.excerpt }}</p>
              </li>
            </ul>
            <p v-if="developerLast.security.tainted_sources?.length" class="security-line">可疑来源：{{ developerLast.security.tainted_sources.join('、') }}</p>
            <p v-if="developerLast.security.policy" class="security-line security-policy">策略：{{ developerLast.security.policy }}</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Memory</span><h2>记忆使用</h2></div></div>
            <dl v-if="developerLast" class="detail-list">
              <div><dt>最近消息</dt><dd>{{ developerLast.memory_used?.recent_messages || 0 }}</dd></div>
              <div><dt>历史召回</dt><dd>{{ developerLast.memory_used?.history_hits || 0 }}</dd></div>
              <div><dt>会话摘要</dt><dd>{{ developerLast.memory_used?.summary_used ? '已使用' : '未使用' }}</dd></div>
              <div><dt>用户画像</dt><dd>{{ developerLast.memory_used?.profile_used ? '已使用' : '未使用' }}</dd></div>
              <div><dt>模拟工单</dt><dd>{{ developerLast.ticket_id || '-' }}</dd></div>
              <div><dt>本轮工具耗时</dt><dd>{{ developerLast.metrics?.tool_latency_ms ?? '-' }} ms</dd></div>
              <div><dt>本轮模型耗时</dt><dd>{{ developerLast.metrics?.model?.latency_ms ?? '-' }} ms</dd></div>
              <div><dt>模型 Token（入 / 出）</dt><dd>{{ developerLast.metrics?.model?.input_tokens ?? '未返回' }} / {{ developerLast.metrics?.model?.output_tokens ?? '未返回' }}</dd></div>
            </dl>
            <p v-else class="side-empty">同一会话的下一轮请求会读取记忆和未完成计划。</p>
          </section>
        </aside>
      </div>
    </section>

  </main>
</template>
<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { backendMeta, createInitialSettings, login, requestDeveloperChat, requestHealth, requestRuns, requestRun, saveSettings } from './lib/backends'
const settings = reactive(createInitialSettings())
const token = ref('')
const username = ref('')
const password = ref('')
const loginError = ref('')
const loginBusy = ref(false)
const developerMessages = ref([])
const developerDraft = ref('')
const developerBusy = ref(false)
const developerLast = ref(null)
const developerMessageList = ref(null)
const runs = ref([])
const historyError = ref('')
let currentController = null
const retryRequest = ref(null)
const developerHealthOk = ref(false)
const developerDocsUrl = computed(() => `${backendMeta('python', settings).baseUrl}/docs`)
let messageSequence = 0
// 诊断结论字段可能由新旧后端版本返回，映射时保留未知值兜底。
const HYPOTHESIS_STATUS_LABELS = { supported: '已支持', contradicted: '被证据否定', observed: '仅观察到' }
const CONFIDENCE_LABELS = { high: '高', medium: '中', low: '低' }
const DIAGNOSIS_STATUS_LABELS = { evidence_available: '证据可支持', conflicting_evidence: '证据冲突', insufficient_evidence: '证据不足' }
const LINKAGE_ASSESSMENT_LABELS = {
  related_request_succeeded: '关联请求已成功',
  correlated_unproven: '有关联但未证实',
  related_request_failed_other: '关联请求其他原因失败'
}
const SECURITY_KIND_LABELS = {
  instruction_override: '指令覆盖',
  system_prompt_leak: '系统提示泄露',
  credential_exfiltration: '凭证外泄诱导',
  authorization_bypass: '绕过授权',
  role_injection: '角色注入'
}
function hypothesisStatusLabel(value) { return HYPOTHESIS_STATUS_LABELS[value] || value || '未知状态' }
function confidenceLabel(value) { return CONFIDENCE_LABELS[value] || value || '未知' }
function diagnosisStatusLabel(value) { return DIAGNOSIS_STATUS_LABELS[value] || value || '证据状态未知' }
function linkageAssessmentLabel(value) { return LINKAGE_ASSESSMENT_LABELS[value] || value || '关联分析' }
function securityKindLabel(value) { return SECURITY_KIND_LABELS[value] || value || '未知类型' }
watch(() => settings.developerConversationId, () => saveSettings(settings))
onMounted(async () => { try { developerHealthOk.value = (await requestHealth('python', settings)).status === 'ok' } catch { developerHealthOk.value = false } })
async function signIn() {
  loginBusy.value = true; loginError.value = ''
  try { const result = await login(settings, username.value, password.value); clearDeveloperConversation(); token.value = result.access_token; password.value = ''; await refreshRuns() }
  catch (error) { loginError.value = error.message }
  finally { loginBusy.value = false }
}
function logout() { currentController?.abort(); token.value = ''; password.value = ''; runs.value = []; historyError.value = ''; clearDeveloperConversation() }
async function refreshRuns() {
  try { runs.value = (await requestRuns(settings, token.value)).runs; historyError.value = '' }
  catch (error) { historyError.value = error.message }
}
async function openRun(runId) {
  try {
    const result = await requestRun(settings, token.value, runId)
    retryRequest.value = null
    settings.developerConversationId = result.conversation_id
    developerLast.value = result
    developerMessages.value = [{ id: ++messageSequence, role: 'assistant', content: result.answer, runId: result.run_id }]
    historyError.value = ''
  } catch (error) { historyError.value = error.message }
}
function cancelRun() { currentController?.abort() }
function clearDeveloperConversation() { developerMessages.value = []; developerLast.value = null; settings.developerConversationId = ''; retryRequest.value = null }
async function retryLastRequest() { if (retryRequest.value) await sendDeveloperMessage(retryRequest.value) }
async function sendDeveloperMessage(retry = null) {
  // Vue passes a submit event for the regular form action.
  const prior = retry && typeof retry.requestId === 'string' ? retry : null
  const content = prior ? prior.content : developerDraft.value.trim()
  if (!content || developerBusy.value) return
  if (!prior) developerMessages.value.push({ id: ++messageSequence, role: 'user', content })
  const request = prior || { content, requestId: crypto.randomUUID(), conversationId: settings.developerConversationId }
  retryRequest.value = null
  developerDraft.value = ''; developerBusy.value = true
  currentController = new AbortController()
  developerLast.value = { timeline: [] }
  try {
    const requestSettings = { ...settings, developerConversationId: request.conversationId }
    const response = await requestDeveloperChat(requestSettings, content, token.value, request.requestId,
      event => developerLast.value?.timeline?.push(event), currentController.signal)
    developerHealthOk.value = true
    if (response.conversation_id) settings.developerConversationId = response.conversation_id
    developerLast.value = response
    developerMessages.value.push({ id: ++messageSequence, role: 'assistant', content: response.answer, runId: response.run_id })
    await refreshRuns()
  } catch (error) {
    retryRequest.value = request
    if (error.name !== 'AbortError') developerHealthOk.value = false
    developerMessages.value.push({ id: ++messageSequence, role: 'assistant', content: error.name === 'AbortError' ? '已停止本次请求；如需继续请重新发送。' : error.message })
  } finally {
    currentController = null
    developerBusy.value = false
    await nextTick()
    developerMessageList.value?.scrollTo({ top: developerMessageList.value.scrollHeight, behavior: 'smooth' })
  }
}
</script>
<style scoped>
.diagnosis-status { color: #92a1f5; font-size: 12px; font-weight: 600; }
.diagnosis-summary { margin: 0 0 12px; color: #9aa7bb; font-size: 12px; line-height: 1.6; }
.hypothesis { padding-bottom: 12px; margin-bottom: 12px; border-bottom: 1px solid rgba(141, 153, 170, 0.16); }
.hypothesis:last-of-type { padding-bottom: 0; margin-bottom: 0; border-bottom: 0; }
.hypothesis-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; margin: 0 0 6px; line-height: 1.5; }
.hypothesis-head strong { color: #e6ebf4; }
.hypothesis-status { padding: 1px 7px; border-radius: 999px; border: 1px solid rgba(141, 153, 170, 0.35); color: #b8c5d7; font-size: 11px; white-space: nowrap; }
.hypothesis-status-supported { border-color: rgba(65, 201, 140, 0.5); color: #7fe0b4; }
.hypothesis-status-contradicted { border-color: rgba(240, 108, 108, 0.55); color: #ff9aa2; }
.hypothesis-status-observed { border-color: rgba(246, 191, 80, 0.5); color: #f4cf82; }
.hypothesis-confidence { color: #9aa7bb; font-size: 11px; }
.hypothesis-line { margin: 0 0 4px; color: #b4bfd0; font-size: 12px; line-height: 1.55; word-break: break-word; }
.hypothesis-contradicted { color: #ffb4ba; }
.section-label { margin: 16px 0 8px; color: #93a3b8; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
.diagnosis-conflict { margin-top: 14px; padding: 10px 12px; border-left: 2px solid rgba(246, 191, 80, 0.7); border-radius: 0 10px 10px 0; background: rgba(246, 191, 80, 0.08); }
.diagnosis-conflict .section-label { margin-top: 0; color: #f4cf82; }
.conflict-item + .conflict-item, .linkage-item + .linkage-item { margin-top: 10px; }
.conflict-item p, .linkage-item p { margin: 0 0 4px; color: #d5dce8; font-size: 12px; line-height: 1.6; }
.conflict-item small, .linkage-item small { color: #9aa7bb; font-size: 11px; word-break: break-all; }
.diagnosis-linkage { margin-top: 14px; padding-left: 12px; border-left: 2px solid rgba(141, 153, 170, 0.35); }
.diagnosis-linkage .section-label { margin-top: 0; }
.security-mark { color: #ffb4ba; font-size: 12px; font-weight: 600; }
.security-clear { color: #7fe0b4; font-size: 12px; font-weight: 600; }
.security-alert { margin: 0 0 10px; padding: 10px 12px; border-left: 2px solid rgba(240, 108, 108, 0.7); border-radius: 0 10px 10px 0; background: rgba(240, 108, 108, 0.08); color: #ffc4c9; font-size: 12px; line-height: 1.6; }
.security-flags { list-style: none; margin: 0 0 10px; padding: 0; display: grid; gap: 8px; }
.security-flags li { display: grid; gap: 3px; padding: 9px 11px; border: 1px solid rgba(141, 153, 170, 0.22); border-radius: 10px; }
.security-flags strong { color: #ffb4ba; font-size: 12px; }
.security-flags small { color: #9aa7bb; font-size: 11px; word-break: break-all; }
.security-excerpt { margin: 0; color: #c9d2df; font-size: 11px; line-height: 1.5; word-break: break-all; }
.security-line { margin: 0 0 6px; color: #b4bfd0; font-size: 12px; line-height: 1.6; word-break: break-word; }
.security-policy { color: #9aa7bb; }
.action-tainted { display: block; margin-top: 4px; color: #f4cf82; }
</style>
