const defaultUrl = window.__ECHOMIND_CONFIG__?.pythonApiUrl || import.meta.env.VITE_PYTHON_API_URL || '/api/python'
export function createInitialSettings() {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem('devtrace.settings') || '{}') } catch { /* use defaults */ }
  return { developerConversationId: saved.developerConversationId || '', endpoints: { python: defaultUrl } }
}
export function saveSettings(settings) { localStorage.setItem('devtrace.settings', JSON.stringify(settings)) }
export function backendMeta(_type, settings) { return { baseUrl: settings.endpoints.python.replace(/\/$/, '') } }
async function requestJson(base, path, options = {}) {
  const response = await fetch(`${base}${path}`, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`)
  return data
}
export function requestHealth(type, settings) { return requestJson(backendMeta(type, settings).baseUrl, '/health') }
export function login(settings, username, password) {
  return requestJson(backendMeta('python', settings).baseUrl, '/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password })
  })
}
export async function requestDeveloperChat(settings, message, token, requestId, onEvent, signal) {
  const response = await fetch(`${backendMeta('python', settings).baseUrl}/developer/chat/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, 'Idempotency-Key': requestId },
    body: JSON.stringify({ message, conversation_id: settings.developerConversationId || undefined }), signal
  })
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `请求失败 (${response.status})`) }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', final = null
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let end
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, end); buffer = buffer.slice(end + 2)
      const kind = block.match(/^event: (.+)$/m)?.[1]
      const text = block.match(/^data: (.+)$/m)?.[1]
      if (!kind || !text) continue
      const data = JSON.parse(text)
      if (kind === 'error') throw new Error(data.detail || '诊断失败')
      if (kind === 'complete') final = data
      if (kind === 'timeline') onEvent(data)
    }
  }
  if (!final) throw new Error('连接中断，未收到完整诊断结果')
  return final
}
