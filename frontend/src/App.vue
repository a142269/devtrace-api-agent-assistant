<template>
  <main :class="['app-shell', `app-shell-${activeView}`]">
    <header class="topbar">
      <a class="brand" href="#" aria-label="EchoMind 首页" @click.prevent="activeView = 'chat'">
        <span class="brand-mark">E</span>
        <span class="brand-name">EchoMind</span>
      </a>

      <nav class="view-nav" aria-label="工作区">
        <button :class="{ active: activeView === 'chat' }" @click="activeView = 'chat'">对话</button>
        <button :class="{ active: activeView === 'developer' }" @click="activeView = 'developer'">开发者助手</button>
        <button :class="{ active: activeView === 'knowledge' }" @click="activeView = 'knowledge'">知识库</button>
        <button :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">评测</button>
      </nav>

      <div class="topbar-tools">
        <span class="environment-pill">
          <i :class="(activeView === 'developer' ? developerHealthOk : healthOk) ? 'online' : 'offline'"></i>
          {{ activeView === 'developer' ? 'Python · 模拟平台' : currentBackend.label }}
        </span>
        <a class="docs-link" :href="activeView === 'developer' ? developerDocsUrl : docsUrl" target="_blank" rel="noreferrer">API 文档</a>
        <button class="avatar-button" title="当前用户">{{ userInitial }}</button>
      </div>
    </header>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>

    <section v-if="activeView === 'chat'" class="page page-chat">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Conversation lab</span>
          <h1>和客服 Agent 对话</h1>
          <p>发送一条真实请求，查看它如何识别意图、选择 Agent 并生成回复。</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.conversationId || '新会话' }}</span>
          <button class="quiet-button" @click="clearConversation">清空</button>
        </div>
      </div>

      <div class="chat-layout">
        <section class="chat-stage">
          <div class="stage-bar">
            <div class="stage-context">
              <span class="context-dot"></span>
              <span>{{ currentBackend.baseUrl }}</span>
            </div>
            <span>{{ messages.length }} 条消息</span>
          </div>

          <div class="messages" ref="messageList">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? '你' : currentBackend.label + ' Agent' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <p>{{ item.content }}</p>
              <div v-if="item.trace" class="message-trace">
                <div class="trace-head">
                  <span>工具调用</span>
                  <small v-if="item.trace.requestId">#{{ item.trace.requestId }}</small>
                </div>
                <div v-if="item.trace.toolCalls?.length" class="trace-calls">
                  <details v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" open>
                    <summary>
                      <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                      <span>{{ call.success ? '成功' : '失败' }}</span>
                    </summary>
                    <pre>{{ formatJson(call.input || {}) }}</pre>
                  </details>
                </div>
                <div v-else class="trace-empty-block">
                  <p>本次请求已生成 trace，但没有可展示的工具输入。</p>
                  <p v-if="item.trace.toolsUsed?.length" class="trace-note">已调用：{{ item.trace.toolsUsed.join(' · ') }}</p>
                </div>
              </div>
            </article>

            <div v-if="messages.length === 0" class="empty-state">
              <div class="empty-symbol">✦</div>
              <h2>从一个客户问题开始</h2>
              <p>下面的快捷问题只是起点，你也可以直接输入自己的测试用例。</p>
              <div class="starter-prompts">
                <button @click="usePrompt('我想申请退款，订单号是 #12345')">退款申请</button>
                <button @click="usePrompt('登录时提示错误，应该怎么排查？')">技术排查</button>
                <button @click="usePrompt('发票多久可以开具？')">发票咨询</button>
              </div>
            </div>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <textarea
              v-model="draft"
              rows="3"
              placeholder="输入消息..."
              @keydown.meta.enter.prevent="sendMessage"
              @keydown.ctrl.enter.prevent="sendMessage"
            ></textarea>
            <div class="composer-bottom">
              <span>⌘ / Ctrl + Enter 发送</span>
              <button type="submit" :disabled="busy || !draft.trim()">{{ busy ? '处理中' : '发送' }}</button>
            </div>
          </form>
        </section>

        <aside class="chat-sidebar" ref="sidebarRef">
          <div class="chat-sidebar-scroll">
            <section class="side-card session-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Session</span>
                  <h2>会话信息</h2>
                </div>
                <span class="status-copy muted">{{ settings.conversationId ? '已启用' : '新会话' }}</span>
              </div>
              <div class="session-grid">
                <div>
                  <span>会话 ID</span>
                  <strong>{{ settings.conversationId || '自动生成' }}</strong>
                </div>
                <div>
                  <span>用户 ID</span>
                  <strong>{{ settings.userId || 'anonymous' }}</strong>
                </div>
              </div>
            </section>

            <section class="side-card connection-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Connection</span>
                  <h2>连接配置</h2>
                </div>
                <span class="status-copy" :class="healthOk ? 'success' : 'muted'">{{ healthLabel }}</span>
              </div>

              <div class="backend-tabs">
                <button :class="{ active: settings.backend === 'java' }" @click="switchBackend('java')">Java</button>
                <button :class="{ active: settings.backend === 'python' }" @click="switchBackend('python')">Python</button>
              </div>

              <label>
                <span>用户 ID</span>
                <input v-model="settings.userId" @change="persist" placeholder="u1001" />
              </label>
              <label>
                <span>会话 ID</span>
                <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
              </label>
              <div class="side-actions">
                <button @click="checkHealth">检查连接</button>
                <button class="quiet-button" @click="refreshConsole">刷新</button>
              </div>
            </section>

            <section class="side-card trace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Last trace</span>
                  <h2>最近一次请求</h2>
                </div>
                <span class="trace-status" :class="lastResponse ? 'has-data' : ''"></span>
              </div>

              <div v-if="lastResponse" class="trace-body">
                <div class="latency">
                  <span>响应耗时</span>
                  <strong>{{ lastResponse.latencyMs || '-' }}<small> ms</small></strong>
                </div>
                <dl class="detail-list">
                  <div><dt>主 Agent</dt><dd>{{ lastResponse.primaryAgent || lastResponse.agentType || '-' }}</dd></div>
                  <div><dt>意图</dt><dd>{{ lastResponse.intent || '-' }}</dd></div>
                  <div><dt>置信度</dt><dd>{{ formatPercent(lastResponse.routingConfidence) }}</dd></div>
                  <div><dt>知识库</dt><dd :class="lastResponse.knowledgeUsed ? 'success' : 'muted'">{{ lastResponse.knowledgeUsed ? '已使用' : '未使用' }}</dd></div>
                  <div><dt>转人工</dt><dd :class="lastResponse.escalated ? 'danger' : 'muted'">{{ lastResponse.escalated ? '是' : '否' }}</dd></div>
                </dl>
                <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
                <div v-if="lastTrace?.trace" class="trace-call-list">
                  <div class="trace-call-title">工具调用</div>
                  <div v-for="(call, index) in lastTrace.trace.toolCalls" :key="`${call.tool_use_id || index}`" class="trace-call-item">
                    <div class="trace-call-meta">
                      <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                      <span>{{ call.latency_ms || 0 }} ms</span>
                    </div>
                    <pre>{{ formatJson(call.input || {}) }}</pre>
                  </div>
                  <div v-if="!lastTrace.trace.toolCalls?.length" class="trace-empty-block">
                    <p>这次 trace 没有记录到工具输入。</p>
                    <p v-if="lastTrace.trace.toolsUsed?.length" class="trace-note">已调用：{{ lastTrace.trace.toolsUsed.join(' · ') }}</p>
                  </div>
                </div>
              </div>
              <p v-else class="side-empty">发送消息后，这里会显示 Agent 路由、意图和耗时。</p>
            </section>

            <section class="side-card monitor-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Runtime</span>
                  <h2>运行状态</h2>
                </div>
                <button class="link-button" @click="loadMonitor">刷新</button>
              </div>
              <div class="mini-stats">
                <div><strong>{{ totalRequests }}</strong><span>请求</span></div>
                <div><strong>{{ agentCount }}</strong><span>Agent</span></div>
                <div><strong>{{ activeAlerts.length }}</strong><span>告警</span></div>
              </div>
              <div v-if="activeAlerts.length" class="alert-note">{{ activeAlerts[0].detail || activeAlerts[0].title }}</div>
              <p v-else class="healthy-note">当前没有活跃告警。</p>
            </section>
          </div>
        </aside>
      </div>
    </section>

    <section v-else-if="activeView === 'developer'" class="page page-developer">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Developer support lab</span>
          <h1>API 接入诊断助手</h1>
          <p>查看 Agent 如何制定计划、查询模拟平台记录，并依据观察结果完成排查。</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.developerConversationId || '新会话' }}</span>
          <button class="quiet-button" @click="clearDeveloperConversation">清空</button>
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
              <button type="submit" :disabled="developerBusy || !developerDraft.trim()">{{ developerBusy ? '排查中' : '发送' }}</button>
            </div>
          </form>
        </section>
        <aside class="developer-inspector">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Plan</span><h2>执行计划</h2></div><strong>{{ developerLast?.plan?.status || '待开始' }}</strong></div>
            <p v-if="developerLast?.plan?.goal" class="developer-goal">{{ developerLast.plan.goal }}</p>
            <p v-if="developerLast" class="developer-goal">决策来源：{{ developerLast.decision_mode === 'model' ? '配置模型' : developerLast.decision_mode === 'rules_fallback' ? '规则降级（模型决策未完成）' : '本地规则模型' }}</p>
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

    <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Knowledge operations</span>
          <h1>知识库</h1>
          <p>搜索、补充和维护客服 Agent 使用的知识片段。</p>
        </div>
        <div class="count-display"><strong>{{ knowledgeCount }}</strong><span>chunks</span></div>
      </div>

      <div class="knowledge-layout">
        <section class="workspace-card search-workspace">
          <div class="card-heading">
            <div><span class="kicker">Retrieval</span><h2>检索知识</h2></div>
            <code>POST /search</code>
          </div>
          <div class="search-line">
            <input v-model="searchQuery" placeholder="例如：退款多久到账" @keydown.enter="searchKnowledge" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">搜索</button>
          </div>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <div class="result-title"><strong>{{ item.title || '未命名文档' }}</strong><small>score {{ item.score ?? '-' }}</small></div>
                <p>{{ item.content }}</p>
              </div>
            </article>
          </div>
          <div v-else class="workspace-empty">输入客户问题开始搜索。</div>
        </section>

        <section class="workspace-card import-workspace">
          <div class="card-heading">
            <div><span class="kicker">Ingestion</span><h2>添加知识</h2></div>
            <code>ChromaDB</code>
          </div>
          <label><span>标题</span><input v-model="docTitle" placeholder="退款补充政策" /></label>
          <label><span>内容</span><textarea v-model="docContent" rows="7" placeholder="输入客服规范、产品说明或排障流程"></textarea></label>
          <div class="side-actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="upload-button">上传文件<input type="file" accept=".txt,.md,.json" @change="handleUpload" /></label>
          </div>
        </section>
      </div>

      <section class="workspace-card skills-workspace">
        <div class="card-heading">
          <div><span class="kicker">Loaded skills</span><h2>已加载能力</h2></div>
          <button class="link-button" @click="reloadSkillSet">重新加载</button>
        </div>
        <div class="skill-table">
          <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
            <span class="skill-dot"></span><strong>{{ skill.name }}</strong><span>{{ skill.description || '业务规范能力' }}</span><small>{{ skill.content_chars || 0 }} chars</small>
          </div>
          <div v-if="!skillsData.skills.length" class="workspace-empty">暂无已加载 Skill。</div>
        </div>
      </section>
    </section>

    <section v-else class="page page-evaluation">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Evaluation lab</span>
          <h1>评测 Agent</h1>
          <p>运行 FastAPI 内置评测，查看意图识别、对话质量和回归结果。</p>
        </div>
        <button @click="runEvaluation" :disabled="busy">{{ busy ? '运行中...' : '运行评测' }}</button>
      </div>

      <div v-if="evalData" class="evaluation-content">
        <div class="evaluation-summary">
          <div class="score-hero"><span>Pass rate</span><strong>{{ formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.total }} cases passed</small></div>
          <div><span>通过</span><strong>{{ evalData.passed }}</strong></div>
          <div><span>总数</span><strong>{{ evalData.total }}</strong></div>
          <div><span>回归</span><strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong></div>
        </div>
        <div class="evaluation-layout">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Scores</span><h2>平均评分</h2></div></div>
            <div class="score-list">
              <div v-for="(value, key) in evalData.avg_scores" :key="key"><span>{{ key }}</span><i><b :style="{ width: `${Math.min(Number(value) * 10, 100)}%` }"></b></i><strong>{{ Number(value).toFixed(2) }}</strong></div>
            </div>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Recommendations</span><h2>优化建议</h2></div></div>
            <div v-if="evalData.recommendations?.length" class="recommendations"><p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p></div>
            <div v-else class="workspace-empty">本次评测没有返回额外建议。</div>
          </section>
        </div>
      </div>
      <div v-else class="evaluation-empty"><div class="empty-symbol">◎</div><h2>还没有评测结果</h2><p>点击右上角运行一次评测。</p></div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  reloadSkills,
  requestChat,
  requestDeveloperChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  requestToolTrace,
  requestSkills,
  runEvaluation as requestEvaluation,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

const settings = reactive(createInitialSettings())
const activeView = ref('chat')
const messages = ref([])
const developerMessages = ref([])
const developerDraft = ref('')
const developerBusy = ref(false)
const developerLast = ref(null)
const developerMessageList = ref(null)
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const developerHealthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('退款多久能到账')
const searchResults = ref([])
const docTitle = ref('退款补充政策')
const docContent = ref('大促期间退款审核时间可能延长到 3-5 个工作日。')
const messageList = ref(null)
const sidebarRef = ref(null)
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const evalData = ref(null)
const toast = ref('')
let toastTimer
let messageSequence = 0
let sidebarObserver

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const developerDocsUrl = computed(() => `${backendMeta('python', settings).baseUrl}/docs`)
const userInitial = computed(() => (settings.userId || 'U').slice(0, 1).toUpperCase())
const activeAlerts = computed(() => monitorData.value.active_alerts || [])
const agentCount = computed(() => Object.keys(monitorData.value.agent_stats || {}).length)
const totalRequests = computed(() => Object.values(monitorData.value.agent_stats || {}).reduce((sum, item) => sum + Number(item.total || 0), 0))

watch(() => settings.conversationId, persist)
watch(() => settings.developerConversationId, persist)
watch(activeView, (view) => { if (view === 'developer') checkDeveloperHealth() })
onMounted(() => {
  refreshConsole()
  updateSidebarHeight()
  if (typeof ResizeObserver !== 'undefined') {
    sidebarObserver = new ResizeObserver(updateSidebarHeight)
    if (sidebarRef.value) sidebarObserver.observe(sidebarRef.value)
  }
  window.addEventListener('resize', updateSidebarHeight)
})

onBeforeUnmount(() => {
  sidebarObserver?.disconnect?.()
  window.removeEventListener('resize', updateSidebarHeight)
})

function persist() { saveSettings(settings) }

function updateSidebarHeight() {
  const sidebar = sidebarRef.value
  if (!sidebar) return
  const rect = sidebar.getBoundingClientRect()
  const height = Math.max(320, Math.floor(rect.height))
  sidebar.style.setProperty('--sidebar-height', `${height}px`)
}

function switchBackend(type) {
  settings.backend = type
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  messages.value = []
  searchResults.value = []
  lastResponse.value = null
  lastTrace.value = null
  refreshConsole()
}

async function refreshConsole() {
  await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills()])
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings.backend, settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message

    // When the saved backend is stale, try the other configured service once.
    const fallback = settings.backend === 'python' ? 'java' : 'python'
    if (settings.backend !== fallback) {
      try {
        const fallbackData = await requestHealth(fallback, settings)
        if (fallbackData.status === 'ok') {
          settings.backend = fallback
          persist()
          healthOk.value = true
          healthLabel.value = fallbackData.status
          statusText.value = JSON.stringify(fallbackData, null, 2)
          await Promise.allSettled([loadStats(), loadMonitor(), loadSkills()])
        }
      } catch {
        // Keep the original error visible when both services are unavailable.
      }
    }
  }
}

async function checkDeveloperHealth() {
  try {
    const data = await requestHealth('python', settings)
    developerHealthOk.value = data.status === 'ok'
  } catch {
    developerHealthOk.value = false
  }
}

async function loadStats() {
  try {
    const data = await requestKnowledgeStats(settings.backend, settings)
    knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch {
    knowledgeCount.value = '-'
  }
}

async function loadMonitor() {
  try {
    monitorData.value = await requestMonitor(settings.backend, settings)
  } catch {
    monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  }
}

async function loadSkills() {
  try {
    skillsData.value = await requestSkills(settings.backend, settings)
  } catch {
    skillsData.value = { count: 0, skills: [], errors: [] }
  }
}

async function reloadSkillSet() {
  busy.value = true
  try {
    skillsData.value = await reloadSkills(settings.backend, settings)
    showToast('Skills 已重新加载')
  } catch (error) {
    statusText.value = error.message
    showToast('Skills 加载失败')
  } finally { busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings.backend, settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    lastResponse.value = response
    lastTrace.value = await loadToolTrace(response.requestId)
    const meta = [response.intent, response.primaryAgent || response.agentType, response.knowledgeUsed ? 'RAG' : '', response.escalated ? '转人工' : ''].filter(Boolean).join(' · ')
    messages.value.push({ id: createMessageId(), role: 'assistant', content: response.response, meta, trace: lastTrace.value?.trace || null })
    await loadMonitor()
  } catch (error) {
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: '请求失败' })
  } finally {
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function usePrompt(prompt) { draft.value = prompt }

function clearConversation() {
  messages.value = []
  lastResponse.value = null
  lastTrace.value = null
  settings.conversationId = ''
  persist()
}

function clearDeveloperConversation() {
  developerMessages.value = []
  developerLast.value = null
  settings.developerConversationId = ''
  persist()
}

async function sendDeveloperMessage() {
  const content = developerDraft.value.trim()
  if (!content || developerBusy.value) return
  developerMessages.value.push({ id: createMessageId(), role: 'user', content })
  developerDraft.value = ''
  developerBusy.value = true
  try {
    const response = await requestDeveloperChat(settings, content)
    developerHealthOk.value = true
    if (response.conversation_id) {
      settings.developerConversationId = response.conversation_id
      persist()
    }
    developerLast.value = response
    developerMessages.value.push({ id: createMessageId(), role: 'assistant', content: response.answer, runId: response.run_id })
  } catch (error) {
    developerHealthOk.value = false
    developerMessages.value.push({ id: createMessageId(), role: 'assistant', content: error.message })
  } finally {
    developerBusy.value = false
    await nextTick()
    developerMessageList.value?.scrollTo({ top: developerMessageList.value.scrollHeight, behavior: 'smooth' })
  }
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value, 5)
    searchResults.value = data.results || []
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    statusText.value = error.message
    showToast('检索失败，请检查连接')
  } finally { busy.value = false }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(settings.backend, settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    showToast('文档已添加')
  } catch (error) {
    statusText.value = error.message
    showToast('文档导入失败')
  } finally { busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(settings.backend, settings, file)
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    showToast(`${file.name} 导入成功`)
  } catch (error) {
    statusText.value = error.message
    showToast('文件导入失败')
  } finally { busy.value = false }
}

async function runEvaluation() {
  busy.value = true
  try {
    evalData.value = await requestEvaluation(settings.backend, settings)
    showToast('评测完成')
  } catch (error) {
    statusText.value = error.message
    showToast('评测运行失败')
  } finally { busy.value = false }
}

async function loadToolTrace(requestId) {
  try {
    return await requestToolTrace(settings.backend, settings, requestId)
  } catch {
    return null
  }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
