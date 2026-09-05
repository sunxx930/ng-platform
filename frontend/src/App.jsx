import { useEffect, useState } from 'react'
import './App.css'

const EVENT_LABEL = {
  'project.created': '创建项目', 'task.created': '创建任务', 'task.state_changed': '状态流转',
  'deliverable.submitted': '提交产出', 'review.requested': '请求复核', 'review.decided': '复核结论',
  'approval.requested': '请求审批', 'approval.decided': '审批决策', 'agent.assigned': '指派 Agent',
  'agent.transferred': '转移任务', 'agent.registered': '注册 Agent', 'goal.parsed': '解析需求',
  'message.aggregated': '收到消息', 'feedback.submitted': '收到反馈', 'project.paused': '项目暂停',
  'project.archived': '项目终止',
}
function evSummary(e) {
  const p = e.payload || {}
  const s = STATUS[p.to]?.cn || STATUS[p.to] || ''
  const map = {
    'task.state_changed': p.to ? `→ ${s}` : '',
    'deliverable.submitted': p.file_ref ? `产出：${p.file_ref}` : '',
    'agent.assigned': p.agent ? `给 ${p.agent}` : '',
    'goal.parsed': p.summary ? `已拆出 ${(p.tasks || []).length} 个任务` : '',
    'approval.decided': p.result === 'approve' ? '已批准' : '已拒绝',
    'review.decided': p.verdict ? `结论：${p.verdict}` : '',
  }
  return map[e.event_type] || ''
}

const LEGAL = {
  todo: ['in_progress', 'cancelled'],
  in_progress: ['in_review', 'blocked', 'cancelled'],
  blocked: ['in_progress', 'cancelled'],
  in_review: ['in_progress', 'pending_approval'],      // completed 仅由复核/审批触发（P0-1）
  pending_approval: ['in_progress', 'cancelled'],
}

// 数值型任务检测（防投诉护栏 2026-09-03）：标题含数值计算词 → 复核需核对算式
const NUMERIC_WORDS = ['计算', '统计', '权重', '比率', '胜率', '均值', '检验', '概率', '金额', '算', '汇总', '指标']
function isNumericTask(title = '') {
  return NUMERIC_WORDS.some((w) => title.includes(w))
}
const STATUS = {
  todo: { cn: '待办', cls: 'todo' },
  in_progress: { cn: '进行中', cls: 'doing' },
  in_review: { cn: '待复核', cls: 'review' },
  pending_approval: { cn: '待审批', cls: 'approval' },
  blocked: { cn: '阻塞', cls: 'blocked' },
  completed: { cn: '完成', cls: 'done' },
  cancelled: { cn: '取消', cls: 'cancel' },
}

function api(path, token, opts) {
  return fetch('/api' + path, {
    ...opts,
    headers: { Authorization: `Bearer ${token}`, ...(opts?.headers || {}) },
  }).then((r) => { if (!r.ok) throw { status: r.status }; return r.json() })
}

function errMsg(e) {
  if (e?.status === 401) return '🔒 未认证：token 无效或已过期'
  if (e?.status === 403) return '🚫 权限不足：当前 token 级别不够，需要 L3'
  if (e?.status === 409) return '⚠️ 冲突：需审批或幂等键冲突'
  if (e?.status === 404) return '❓ 未找到'
  return String(e?.message || e || '未知错误')
}

function AuthScreen({ onAuth }) {
  const [mode, setMode] = useState('login')   // login | register
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  function submit(e) {
    e.preventDefault()
    setBusy(true); setErr('')
    fetch(`/api/auth/${mode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}))
        if (!r.ok) throw { status: r.status, detail: d.detail || '请求失败' }
        return d
      })
      .then((d) => onAuth({ token: d.token, user_id: d.user_id, username: d.username, level: d.level }))
      .catch((e) => setErr(String(e.detail || e?.message || '网络错误')))
      .finally(() => setBusy(false))
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="brand auth-brand">
          <div className="logo">🐳</div>
          <div className="brand-text">
            <div className="brand-name"><span className="accent">NG</span> AI Platform</div>
            <div className="tagline">给一个目标，得到你想要的</div>
          </div>
        </div>
        <h2 className="auth-title">{mode === 'register' ? '注册新账号' : '登录'}</h2>
        <p className="muted auth-sub">
          {mode === 'register' ? '注册后即可进入平台，创建属于你的项目' : '用已注册的账号登录，进入你的项目'}
        </p>
        <form onSubmit={submit} className="auth-form">
          <input data-testid="auth-username" placeholder="用户名" autoComplete="username"
                 value={username} onChange={(e) => setUsername(e.target.value)} />
          <input data-testid="auth-password" type="password" placeholder="密码（至少 6 位）" autoComplete="current-password"
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          {err && <div className="error auth-err" data-testid="auth-error"><span>{err}</span></div>}
          <button className="primary big" data-testid="auth-submit" type="submit"
                  disabled={busy || !username.trim() || password.length < 6}>
            {busy ? '请稍候…' : mode === 'register' ? '注册并进入' : '登录进入'}
          </button>
        </form>
        <div className="auth-switch">
          {mode === 'login' ? (
            <span>还没有账号？<button className="link" data-testid="auth-to-register"
                                     onClick={() => { setMode('register'); setErr('') }}>去注册</button></span>
          ) : (
            <span>已有账号？<button className="link" data-testid="auth-to-login"
                                   onClick={() => { setMode('login'); setErr('') }}>去登录</button></span>
          )}
        </div>
      </div>
    </div>
  )
}

function App() {
  const [session, setSession] = useState(() => {
    // 测试直通：URL 带 ?demo=<token> → 免注册直接以 demo 身份进入（测试 agent 用）。
    // token 形如 demo-<名字>-xxx → 显示名取名字段（如 demo-lobster-x → "lobster"）
    try {
      const dp = new URLSearchParams(window.location.search).get('demo')
      if (dp) {
        const name = dp.startsWith('demo-')
          ? (dp.split('-')[1] || 'demo').replace(/^./, (c) => c.toUpperCase())
          : 'demo'
        localStorage.setItem('ng_session', JSON.stringify({ token: dp, username: name, level: 3 }))
        return { token: dp, username: name, level: 3 }
      }
      return JSON.parse(localStorage.getItem('ng_session'))
    } catch { return null }
  })
  const token = session?.token || ''
  const isDemo = typeof token === 'string' && token.startsWith('demo-')
  const [projects, setProjects] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [agents, setAgents] = useState([])
  const [templates, setTemplates] = useState([])
  const [error, setError] = useState('')
  const [llm, setLlm] = useState({ provider: 'qwen', api_key: '', model: '', base_url: '' })   // 默认千问（开箱即用 B 方案）
  const [llmCurrent, setLlmCurrent] = useState(null)
  const [needGuide, setNeedGuide] = useState(false)   // 无 key → 显示千问免费引导
  const [providers, setProviders] = useState([])
  const [usage, setUsage] = useState(null)
  const [goal, setGoal] = useState('')
  const [goalTitle, setGoalTitle] = useState('')
  const [goalLoading, setGoalLoading] = useState(false)
  const [showFb, setShowFb] = useState(false)
  const [fb, setFb] = useState({ content: '', contact: '', rating: null })
  const [toast, setToast] = useState('')
  const [confirmDel, setConfirmDel] = useState(null)
  const [notifs, setNotifs] = useState([])
  const [showNotifs, setShowNotifs] = useState(false)
  const canL3 = (session?.level || 0) >= 3
  const authed = !!session?.token
  const [opinions, setOpinions] = useState({})   // v1.1：打回修改意见（复核需填）

  // 会话校验：token 无效/过期 → 清会话回登录页
  useEffect(() => {
    if (!authed) return
    api('/auth/me', token).catch(() => { localStorage.removeItem('ng_session'); setSession(null) })
  }, [authed, token])

  function loadProjects() {
    api('/projects', token).then((d) => { setProjects(d.projects || []); setError('') }).catch((e) => setError(errMsg(e)))
  }
  function refreshNotifs() {
    api('/notifications', token).then((d) => setNotifs(d.notifications || [])).catch(() => {})
  }
  useEffect(() => {
    if (!authed) return
    api('/projects', token).then((d) => { setProjects(d.projects || []); setError('') }).catch((e) => setError(errMsg(e)))
  }, [authed, token])

  function loadDetail(pid) {
    setSelected(pid)
    setDetail(null)
    Promise.all([api(`/projects/${pid}/tasks`, token), api(`/projects/${pid}/audit`, token)])
      .then(([t, a]) => { setDetail({ tasks: t.tasks || [], audit: a.events || [] }); setError('') })
      .catch((e) => setError(errMsg(e)))
  }

  useEffect(() => { if (authed) api('/agents', token).then((d) => setAgents(d.agents || [])).catch(() => {}) }, [authed, token])
  useEffect(() => { if (authed) api('/agents/templates', token).then((d) => setTemplates(d.templates || [])).catch(() => {}) }, [authed, token])
  useEffect(() => {
    if (!authed) return
    api('/agents/llm-config', token).then((d) => {
      setLlmCurrent(d)
      // B 方案（2026-09-04）：无已保存 key → 默认千问 + 显示免费引导
      if (!d?.api_key_set) {
        setNeedGuide(true)
        setLlm((prev) => ({ ...prev, provider: 'qwen', model: 'qwen-max', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' }))
      }
    }).catch(() => {})
  }, [authed, token])
  useEffect(() => { if (authed) api('/agents/providers', token).then((d) => setProviders(d.providers || [])).catch(() => {}) }, [authed, token])
  useEffect(() => { if (authed) api('/usage', token).then(setUsage).catch(() => {}) }, [authed, token])
  useEffect(() => {
    if (!authed) return
    api('/notifications', token).then((d) => setNotifs(d.notifications || [])).catch(() => {})
  }, [authed, token])

  const fmtTokens = (n) => n >= 1000000 ? `${(n / 1000000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
  const usedTokens = usage ? (usage.input_tokens || 0) + (usage.output_tokens || 0) : 0
  const limit = usage?.context_limit || 1000000
  const usagePct = Math.min(100, (usedTokens / limit) * 100)

  function submitGoal() {
    if (!goal.trim()) return
    setGoalLoading(true); setError('')
    const q = new URLSearchParams({ title: goalTitle || '新需求', goal }).toString()
    api(`/projects?${q}`, token, { method: 'POST' })
      .then((d) => {
        const g = new URLSearchParams({ body: goal, parse: 'true' }).toString()
        return api(`/projects/${d.project_id}/messages?${g}`, token, { method: 'POST' })
      })
      .then(() => { loadProjects(); setGoal(''); setGoalTitle(''); setGoalLoading(false) })
      .catch((e) => { setError(errMsg(e)); setGoalLoading(false) })
  }

  function archiveProject(pid) {
    api(`/projects/${pid}/archive`, token, { method: 'POST' }).then(() => { loadProjects(); setConfirmDel(null); showToast('项目已终止删除') }).catch((e) => setError(errMsg(e)))
  }

  const ngAgents = agents.filter((a) => (a.executor || 'builtin') === 'builtin' && a.status !== 'disabled' && a.name !== 'ng-assistant')
  const capName = (n) => String(n || '').split(/[-_\s]+/).filter(Boolean)
    .map((w) => (w.toLowerCase() === 'ng' ? 'NG' : w.charAt(0).toUpperCase() + w.slice(1))).join(' ')
  const regNames = new Set(ngAgents.map((a) => a.name))
  const combined = [
    ...ngAgents.map((a) => ({ key: 'r-' + a.name, name: capName(a.name), desc: a.capability || a.role || '', reg: a })),
    ...templates.filter((t) => !regNames.has(t.name) && !regNames.has(t.name_cn))
      .map((t) => ({ key: 't-' + t.id, id: t.id, name: t.name, desc: t.desc })),
  ].sort((a, b) => (a.name === 'NG助理' ? -1 : b.name === 'NG助理' ? 1 : 0))

  function showToast(msg) { setToast(msg); setTimeout(() => setToast(''), 2200) }

  function handleAuth(s) {
    localStorage.setItem('ng_session', JSON.stringify(s))
    setSession(s)
    setError('')
  }

  function logout() {
    api('/auth/logout', token, { method: 'POST' }).catch(() => {})
    localStorage.removeItem('ng_session')
    setSession(null)
  }

  function deactivateAgent(name) {
    api(`/agents/${encodeURIComponent(name)}/deactivate`, token, { method: 'POST' })
      .then(() => { api('/agents', token).then((d) => setAgents(d.agents || [])); showToast(`已移除 ${name}，可在模板里重新添加`) })
      .catch((e) => setError(errMsg(e)))
  }

  function addAgent(item) {
    const done = () => api('/agents', token).then((d) => setAgents(d.agents || []))
    if (item.id) {
      return fetch(`/api/agents/templates/${item.id}/instantiate`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
        .then(() => { done(); showToast(`已添加 ${item.name}——提需求时提到它的领域，就会自动派活`) })
        .catch((e) => setError(errMsg(e)))
    }
    const q = new URLSearchParams({ name: item.reg.name, capability: item.reg.capability || '', role: item.reg.role || '', executor: 'builtin' })
    return fetch(`/api/agents/register?${q}`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      .then(() => { done(); showToast(`${item.name} 已在平台`) })
      .catch((e) => setError(errMsg(e)))
  }

  function saveLlm() {
    fetch('/api/agents/llm-config', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(llm) })
      .then((r) => r.json())
      .then(() => { setLlmCurrent({ provider: llm.provider, model: llm.model, api_key_set: !!llm.api_key }); setLlm({ ...llm, api_key: '' }) })
      .catch((e) => setError(errMsg(e)))
  }

  const approvals = (detail?.audit || []).filter((e) => ['approval.requested', 'approval.decided'].includes(e.event_type))
  const decidedAids = new Set(approvals.filter((e) => e.event_type === 'approval.decided').map((e) => e.payload?.approval_id))
  const pendingApprovals = approvals.filter((e) => e.event_type === 'approval.requested' && !decidedAids.has(e.payload?.approval_id))
  // 待复核（龙虾反馈 2026-09-02：前端需补复核决策入口）：review.requested 且未 review.decided
  const reviewsAll = (detail?.audit || []).filter((e) => ['review.requested', 'review.decided'].includes(e.event_type))
  const decidedRids = new Set(reviewsAll.filter((e) => e.event_type === 'review.decided').map((e) => e.payload?.review_id))
  const pendingReviews = reviewsAll.filter((e) => e.event_type === 'review.requested' && !decidedRids.has(e.payload?.review_id))
  // 关联任务标题 / 任务对象
  const tidTitle = {}; (detail?.tasks || []).forEach((t) => { tidTitle[t.task_id] = t.title })
  const taskOf = (tid) => (detail?.tasks || []).find((t) => t.task_id === tid)
  const audit = (detail?.audit || []).slice(-20).reverse()

  function decideApproval(aid, result) {
    api(`/approvals/${aid}/decision?result=${result}`, token, { method: 'POST' })
      .then(() => { if (selected) loadDetail(selected); showToast(result === 'approve' ? '✅ 已批准' : '已拒绝') })
      .catch((e) => setError(errMsg(e)))
  }

  function decideReview(rid, verdict) {
    const op = (opinions[rid] || '').trim()
    if ((verdict === 'needs_changes' || verdict === 'reject') && !op) {
      setError('打回（需修改/拒绝）必须填写修改意见 opinion')
      return
    }
    const q = new URLSearchParams({ verdict, ...(op ? { opinion: op } : {}) })
    api(`/reviews/${rid}/decision?${q}`, token, { method: 'POST' })
      .then(() => { if (selected) loadDetail(selected); showToast(verdict === 'pass' ? '✅ 复核通过' : verdict === 'reject' ? '✕ 复核拒绝' : `复核：${verdict}`) })
      .catch((e) => setError(errMsg(e)))
  }

  function submitDeliverable(tid) {
    const fileRef = window.prompt('产出文件路径（需在程序 artifacts 目录内，如 artifacts/<任务>.md）：', 'artifacts/')
    if (!fileRef) return
    const q = new URLSearchParams({ file_ref: fileRef, verdict: 'done' }).toString()
    api(`/tasks/${tid}/deliverables?${q}`, token, { method: 'POST' })
      .then(() => { if (selected) loadDetail(selected); showToast('📄 产出已提交 → 待复核') })
      .catch((e) => setError(errMsg(e)))
  }

  function advanceState(tid, to) {
    const q = new URLSearchParams({ to }).toString()
    api(`/tasks/${tid}/state?${q}`, token, { method: 'PATCH' })
      .then(() => { if (selected) loadDetail(selected); showToast(`已推进到「${STATUS[to]?.cn || to}」`) })
      .catch((e) => setError(errMsg(e)))
  }

  if (!authed) return <AuthScreen onAuth={handleAuth} />

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="logo">🐳</div>
          <div className="brand-text">
            <div className="brand-name"><span className="accent">NG</span> AI Platform</div>
            <div className="tagline">给一个目标，得到你想要的</div>
          </div>
        </div>
        <div className="toolbar">
          <span className="who">👤 {session.username}</span>
          <span className={`level-badge l${session.level}`}>L{session.level}</span>
          <div className="notif-wrap">
            <button className="notif-btn" data-testid="notif-bell"
                    onClick={() => { setShowNotifs(!showNotifs); refreshNotifs() }}
                    title="通知">🔔{notifs.length > 0 && <span className="notif-dot">{notifs.length}</span>}</button>
            {showNotifs && (
              <div className="notif-panel" data-testid="notif-panel">
                {notifs.length === 0 ? <div className="muted">暂无通知</div> : notifs.map((n, i) => (
                  <div className="notif-item" key={i}
                       onClick={() => { if (n.project_id) loadDetail(n.project_id); setShowNotifs(false) }}>
                    <div className="notif-evt">{EVENT_LABEL[n.event_type] || n.event_type}</div>
                    {n.summary && <div className="notif-sum">{n.summary}</div>}
                    <div className="notif-time">{new Date((n.ts || 0) * 1000).toLocaleTimeString()}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
          <button onClick={() => { loadProjects(); refreshNotifs() }}>刷新</button>
          <button className="primary" onClick={() => setShowFb(true)}>反馈</button>
          <button data-testid="logout" onClick={logout}>退出</button>
        </div>
      </header>

      {error && (
        <div className="error" data-testid="error-banner">
          <span>{error}</span>
          <button className="mini" data-testid="dismiss-error" onClick={() => setError('')}>×</button>
        </div>
      )}

      {/* Hero：提需求 */}
      <section className="hero">
        <div className="hero-title">把目标交给平台，剩下它来干</div>
        <div className="hero-sub">描述你想做的事 —— 平台会自动拆成任务、派给合适的 agent、产出并交接复核。</div>
        <input className="goal-title" placeholder="需求标题（可选）" value={goalTitle}
               onChange={(e) => setGoalTitle(e.target.value)} />
        <textarea placeholder="例如：整理本周项目周报，包含进展、风险与下周计划……" rows={2}
                  value={goal} onChange={(e) => setGoal(e.target.value)} />
        <div className="hero-actions">
          <button className="primary big" onClick={submitGoal} disabled={!goal.trim() || goalLoading}>
            {goalLoading ? '正在组队…' : '🚀 提交 → 自动组队开工'}
          </button>
        </div>
      </section>

      <div className="layout">
        {/* 左：项目 + agent + 算力 */}
        <aside className="sidebar">
          <h3>算力</h3>
          <div className="llm">
            {needGuide && (
              <div className="llm-guide" data-testid="llm-guide">
                <div className="llm-guide-t">🚀 还没配算力？推荐用通义千问（有免费额度）</div>
                <ol>
                  <li>打开 <a href="https://dashscope.console.aliyun.com/apiKey" target="_blank" rel="noreferrer">DashScope</a> 用阿里云账号登录（免费注册）</li>
                  <li>在「API-KEY 管理」创建密钥</li>
                  <li>把 key 粘贴到下面 → 点「保存算力配置」即可用</li>
                </ol>
                <div className="llm-guide-note">千问提供免费试用额度，适合入门；要更强可换 OpenAI/Claude 等（需自行购买 API）</div>
              </div>
            )}
            <select value={llm.provider} onChange={(e) => {
              const p = providers.find((x) => x.id === e.target.value)
              setLlm({ provider: e.target.value, api_key: '', model: p?.default_model || '', base_url: p?.base_url || '' })
            }}>
              {['一线', '二线', '本地'].map((tier) => (
                <optgroup key={tier} label={tier}>
                  {providers.filter((p) => p.tier === tier).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <input type="password" placeholder="API key" value={llm.api_key} onChange={(e) => setLlm({ ...llm, api_key: e.target.value })} />
            <input placeholder="模型" value={llm.model} onChange={(e) => setLlm({ ...llm, model: e.target.value })} />
            {llm.base_url && <input placeholder="base_url" value={llm.base_url} onChange={(e) => setLlm({ ...llm, base_url: e.target.value })} />}
            <button onClick={saveLlm}>保存算力配置</button>
            {llmCurrent && <div className="p-sub">当前: {llmCurrent.provider} · {llmCurrent.model || '-'} · key {llmCurrent.api_key_set ? '✓' : '✗'}</div>}
          </div>
          <div className="usage">
            <div className="usage-head">
              <span>上下文用量</span>
              <span>{fmtTokens(usedTokens)} / {fmtTokens(limit)}</span>
            </div>
            <div className="usage-bar"><div className="usage-fill" style={{ width: `${usagePct}%` }} /></div>
            {usagePct > 80 && <div className="usage-warn">⚠ 接近 1M 上限，长任务将自动压缩上下文</div>}
            <div className="p-sub">{usage?.calls || 0} 次调用 · 输入 {fmtTokens(usage?.input_tokens || 0)} · 输出 {fmtTokens(usage?.output_tokens || 0)}</div>
          </div>

          <h3>我的项目</h3>
          {projects.length === 0 ? (
            <div className="empty">还没有项目 —— 上面提个需求，第一个项目就开始了</div>
          ) : (
            <ul>
              {projects.map((p) => (
                <li key={p.project_id} className={p.project_id === selected ? 'active' : ''}
                    onClick={() => loadDetail(p.project_id)}>
                  <div className="p-row">
                    <div>
                      <div className="p-title">{p.title}</div>
                      <div className="p-sub">{p.goal?.slice(0, 26) || '无目标描述'}</div>
                    </div>
                    <div className="p-actions">
                      <span className={`pill ${p.status}`}>{p.status === 'active' ? '运行中' : p.status}</span>
                      <button className="mini" title="终止/删除" data-testid="archive-project"
                              onClick={(e) => { e.stopPropagation(); setConfirmDel(p.project_id) }}>×</button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <h3>Agent</h3>
          <ul className="agents">
            {combined.map((a) => (
              <li key={a.key}>
                <div className="a-name">{a.name}</div>
                <div className="a-desc">{a.desc}</div>
                {a.reg
                  ? <button className="mini remove" title="移除" onClick={() => deactivateAgent(a.reg.name)}>✕</button>
                  : <button className="mini" title="添加到平台" onClick={() => addAgent(a)}>＋</button>}
              </li>
            ))}
          </ul>

        </aside>

        {/* 右：看板 */}
        <main className="content">
          <div className="stats">
            <div className="stat"><div className="stat-num">{projects.length}</div><div className="stat-label">项目</div></div>
            <div className="stat"><div className="stat-num">{combined.length}</div><div className="stat-label">Agent</div></div>
            <div className="stat"><div className="stat-num">{usage?.calls || 0}</div><div className="stat-label">算力调用</div></div>
            <div className="stat"><div className="stat-num">{fmtTokens(usedTokens)}</div><div className="stat-label">Token 已用</div></div>
          </div>

          {!selected ? (
            projects.length === 0 ? (
              <div className="welcome">
                <div className="welcome-icon">🐳</div>
                <div className="welcome-title">👋 欢迎，{session.username} —— 这是你的工作台</div>
                <div className="welcome-sub">在上面「把目标交给平台」，它会自动拆任务、派 agent、产出并复核；每个项目展开就是任务看板 + 审批队列 + 完整审计流</div>
                {isDemo && (
                  <div className="demo-note" data-testid="demo-note">
                    <strong>试用须知</strong>：① 提需求后平台自动拆任务并行开工（~30s 自动产出→待复核）；
                    ② <strong>复核是人工门</strong>——产出到「待复核」后需在待办中心点 通过/需修改/拒绝，不会自动通过；
                    ③ 被判「需修改」的任务会退回进行中，改好后重新提交即可再次复核；④ 数值任务请核对产出引用的输入数据。
                  </div>
                )}
                <button className="primary big" onClick={() => document.querySelector('.goal-title')?.focus()}>🚀 提第一个需求</button>
              </div>
            ) : (
              <div className="proj-grid">
                {projects.map((p) => (
                  <div key={p.project_id} className="proj-card" data-testid="project-card" onClick={() => loadDetail(p.project_id)}>
                    <div className="proj-head">
                      <div className="proj-title">{p.title}</div>
                      <span className={`pill ${p.status}`}>{p.status === 'active' ? '运行中' : p.status}</span>
                    </div>
                    <div className="proj-goal">{p.goal || '无目标描述'}</div>
                    <div className="proj-foot">
                      <span>👀 点击查看任务看板 →</span>
                    </div>
                  </div>
                ))}
              </div>
            )
          ) : !detail ? (
            <div className="loading">加载中…</div>
          ) : (
            <>
              <h2>任务看板</h2>
              <div className="board">
                {Object.entries(STATUS).map(([st, meta]) => {
                  const ts = detail.tasks.filter((t) => t.status === st)
                  return (
                    <div className="col" key={st}>
                      <div className={`col-head ${meta.cls}`}>{meta.cn}<span className="cnt">{ts.length}</span></div>
                      {ts.map((t) => (
                        <div className="card" key={t.task_id}>
                          <div className="t-title">{t.title}</div>
                          <div className="t-meta">
                            {t.owner && <span className="who">👤 {t.owner}</span>}
                            {t.reviewer && <span className="who">🔍 {t.reviewer}</span>}
                            {t.has_deliverable && <span className="tag">📄 产出</span>}
                          </div>
                          {LEGAL[t.status]?.length > 0 && (
                            <div className="t-actions">
                              {t.status === 'in_progress' && (
                                <button className="mini" data-testid="submit-deliverable" onClick={() => submitDeliverable(t.task_id)}>提交产出</button>
                              )}
                              <select className="advance" value=""
                                      onChange={(e) => e.target.value && advanceState(t.task_id, e.target.value)}>
                                <option value="">推进…</option>
                                {LEGAL[t.status].map((s) => <option key={s} value={s}>{STATUS[s].cn}</option>)}
                              </select>
                            </div>
                          )}
                        </div>
                      ))}
                      {ts.length === 0 && <div className="col-empty">—</div>}
                    </div>
                  )
                })}
              </div>

              <h2>待办中心</h2>
              {pendingReviews.length > 0 && (
                <div className="approvals" style={{ marginBottom: 12 }}>
                  {pendingReviews.map((e, i) => {
                    const t = taskOf(e.task_id)
                    const canReview = canL3 || (t && session?.username && t.reviewer === session.username) || true
                    return (
                      <div className="ap-row" key={'rv' + i}>
                        <span className="ap-type">待复核</span>
                        <span className="ap-scope">{tidTitle[e.task_id] || '任务'}</span>
                        {isNumericTask(tidTitle[e.task_id]) && (
                          <span className="ap-num-note" data-testid="review-numeric-note">⚠ 数值任务：请核对算式与输入</span>
                        )}
                        {canReview ? (
                          <>
                            <input className="opinion-in" placeholder="修改意见（打回必填）"
                                   data-testid="review-opinion"
                                   value={opinions[e.payload.review_id] || ''}
                                   onChange={(ev) => setOpinions({ ...opinions, [e.payload.review_id]: ev.target.value })} />
                            <span className="ap-actions">
                              <button className="mini ok" data-testid="review-pass" onClick={() => decideReview(e.payload.review_id, 'pass')}>✅ 通过</button>
                              <button className="mini" data-testid="review-changes" onClick={() => decideReview(e.payload.review_id, 'needs_changes')}>↩ 需修改</button>
                              <button className="mini danger" data-testid="review-reject" onClick={() => decideReview(e.payload.review_id, 'reject')}>✕ 拒绝</button>
                            </span>
                          </>
                        ) : <span className="ap-locked">🔒 仅项目 owner / 指派 reviewer 可复核</span>}
                      </div>
                    )
                  })}
                </div>
              )}
              {pendingApprovals.length === 0 && pendingReviews.length === 0 ? <p className="muted">暂无待办项</p> : (
                <div className="approvals">
                  {pendingApprovals.map((e, i) => (
                    <div className="ap-row" key={i}>
                      <span className="ap-type">待审批</span>
                      <span className="ap-scope">{e.payload?.scope || '流程变更'}</span>
                      {canL3 ? (
                        <span className="ap-actions">
                          <button className="mini ok" data-testid="approve-btn" onClick={() => decideApproval(e.payload.approval_id, 'approve')}>✅ 批准</button>
                          <button className="mini danger" data-testid="reject-btn" onClick={() => decideApproval(e.payload.approval_id, 'reject')}>✕ 拒绝</button>
                        </span>
                      ) : <span className="ap-locked">🔒 需 L3 权限</span>}
                    </div>
                  ))}
                </div>
              )}

              <h2>审计记录</h2>
              <div className="audit">
                {audit.length === 0 ? <div className="muted audit-empty">暂无记录</div> : audit.map((e, i) => (
                  <div className="ev" key={i}>
                    <span className="ev-type">{EVENT_LABEL[e.event_type] || e.event_type}</span>
                    {evSummary(e) && <span className="ev-pay">{evSummary(e)}</span>}
                    <span className="ev-time">{new Date((e.created_at_ts || 0) * 1000).toLocaleTimeString()}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </main>
      </div>

      {toast && <div className="toast">{toast}</div>}

      {confirmDel && (
        <div className="modal" onClick={() => setConfirmDel(null)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <h3>🗑 终止并删除项目？</h3>
            <p className="muted">项目会从看板移除；审计事件保留（append-only），不可恢复。</p>
            <div className="modal-actions">
              <button onClick={() => setConfirmDel(null)}>取消</button>
              <button className="primary" data-testid="confirm-archive" onClick={() => archiveProject(confirmDel)}>确认删除</button>
            </div>
          </div>
        </div>
      )}

      {showFb && (
        <div className="modal" onClick={() => setShowFb(false)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <h3>💬 提意见 / 反馈</h3>
            <textarea placeholder="哪里好用、哪里不好用，或你的建议……" rows={4} value={fb.content}
                      onChange={(e) => setFb({ ...fb, content: e.target.value })} />
            <input placeholder="联系方式（可选）" value={fb.contact} onChange={(e) => setFb({ ...fb, contact: e.target.value })} />
            <div className="stars">{[1, 2, 3, 4, 5].map((s) => (
              <span key={s} className={fb.rating === s ? 'on' : ''} onClick={() => setFb({ ...fb, rating: s })}>★</span>
            ))}</div>
            <div className="modal-actions">
              <button onClick={() => setShowFb(false)}>取消</button>
              <button className="primary" disabled={!fb.content.trim()} onClick={() => {
                fetch('/api/feedback', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(fb) })
                  .then(() => { setShowFb(false); setFb({ content: '', contact: '', rating: null }) })
                  .catch((e) => setError(errMsg(e)))
              }}>提交</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
