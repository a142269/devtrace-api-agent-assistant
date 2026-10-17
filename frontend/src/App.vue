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
            </div>
          </form>
        </section>
        <aside class="developer-inspector">
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
              </details>
            </div>
            <p v-else class="side-empty">这里展示可验证的行动与观察摘要，不展示模型内部推理。</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Evidence</span><h2>证据引用</h2></div></div>
            <div v-for="item in developerLast?.diagnosis?.hypotheses || []" :key="item.code" class="developer-goal">
              <strong>{{ item.description }}</strong> · {{ item.status }}<br>依据：{{ item.evidence.join('、') }}
            </div>
            <ul v-if="developerLast?.evidence?.length" class="developer-evidence">
              <li v-for="(source, index) in developerLast.evidence" :key="`${source}-${index}`">{{ source }}</li>
            </ul>
            <p v-else class="side-empty">成功核验的文档和模拟记录会列在这里。</p>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Memory</span><h2>记忆使用</h2></div></div>
            <dl v-if="developerLast" class="detail-list">
              <div><dt>最近消息</dt><dd>{{ developerLast.memory_used?.recent_messages || 0 }}</dd></div>
              <div><dt>历史召回</dt><dd>{{ developerLast.memory_used?.history_hits || 0 }}</dd></div>
              <div><dt>会话摘要</dt><dd>{{ developerLast.memory_used?.summary_used ? '已使用' : '未使用' }}</dd></div>
              <div><dt>用户画像</dt><dd>{{ developerLast.memory_used?.profile_used ? '已使用' : '未使用' }}</dd></div>
              <div><dt>模拟工单</dt><dd>{{ developerLast.ticket_id || '-' }}</dd></div>
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
import { backendMeta, createInitialSettings, login, requestDeveloperChat, requestHealth, saveSettings } from './lib/backends'
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
let currentController = null
const developerHealthOk = ref(false)
const developerDocsUrl = computed(() => `${backendMeta('python', settings).baseUrl}/docs`)
let messageSequence = 0
watch(() => settings.developerConversationId, () => saveSettings(settings))
onMounted(async () => { try { developerHealthOk.value = (await requestHealth('python', settings)).status === 'ok' } catch { developerHealthOk.value = false } })
async function signIn() {
  loginBusy.value = true; loginError.value = ''
  try { const result = await login(settings, username.value, password.value); clearDeveloperConversation(); token.value = result.access_token; password.value = '' }
  catch (error) { loginError.value = error.message }
  finally { loginBusy.value = false }
}
function logout() { currentController?.abort(); token.value = ''; password.value = ''; clearDeveloperConversation() }
function cancelRun() { currentController?.abort() }
function clearDeveloperConversation() { developerMessages.value = []; developerLast.value = null; settings.developerConversationId = '' }
async function sendDeveloperMessage() {
  const content = developerDraft.value.trim()
  if (!content || developerBusy.value) return
  developerMessages.value.push({ id: ++messageSequence, role: 'user', content })
  developerDraft.value = ''; developerBusy.value = true
  currentController = new AbortController()
  developerLast.value = { timeline: [] }
  try {
    const response = await requestDeveloperChat(settings, content, token.value, crypto.randomUUID(),
      event => developerLast.value?.timeline?.push(event), currentController.signal)
    developerHealthOk.value = true
    if (response.conversation_id) settings.developerConversationId = response.conversation_id
    developerLast.value = response
    developerMessages.value.push({ id: ++messageSequence, role: 'assistant', content: response.answer, runId: response.run_id })
  } catch (error) {
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
