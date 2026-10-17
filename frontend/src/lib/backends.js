const defaultUrl = window.__ECHOMIND_CONFIG__?.pythonApiUrl || import.meta.env.VITE_PYTHON_API_URL || '/api/python'
export function createInitialSettings() {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem('devtrace.settings') || '{}') } catch { /* use defaults */ }
  return { userId: saved.userId || 'u1001', developerConversationId: saved.developerConversationId || '', endpoints: { python: defaultUrl } }
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
export function requestDeveloperChat(settings, message) {
  return requestJson(backendMeta('python', settings).baseUrl, '/developer/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, user_id: settings.userId || 'anonymous', conversation_id: settings.developerConversationId || undefined })
  })
}
