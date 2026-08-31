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

function App() {
  const [token, setToken] = useState('l1-agent-token')
  const [projects, setProjects] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [agents, setAgents] = useState([])
  const [templates, setTemplates] = useState([])
  const [error, setError] = useState('')
  const [llm, setLlm] = useState({ provider: 'openai', api_key: '', model: '', base_url: '' })
  const [llmCurrent, setLlmCurrent] = useState(null)
  const [providers, setProviders] = useState([])
  const [usage, setUsage] = useState(null)
  const [goal, setGoal] = useState('')
  const [goalTitle, setGoalTitle] = useState('')
  const [goalLoading, setGoalLoading] = useState(false)
  const [showFb, setShowFb] = useState(false)
  const [fb, setFb] = useState({ content: '', contact: '', rating: null })
  const [toast, setToast] = useState('')
  const [me, setMe] = useState(null)
  const canL3 = (me?.level || 0) >= 3

  useEffect(() => { api('/auth/me', token).then(setMe).catch(() => setMe(null)) }, [token])

  function loadProjects() { api('/projects', token).then((d) => setProjects(d.projects || [])).catch((e) => setError(errMsg(e))) }
  useEffect(loadProjects, [token])

  function loadDetail(pid) {
    setSelected(pid)
    setDetail(null)
    Promise.all([api(`/projects/${pid}/tasks`, token), api(`/projects/${pid}/audit`, token)])
      .then(([t, a]) => setDetail({ tasks: t.tasks || [], audit: a.events || [] }))
      .catch((e) => setError(errMsg(e)))
  }

  useEffect(() => { api('/agents', token).then((d) => setAgents(d.agents || [])).catch(() => {}) }, [token])
  useEffect(() => { api('/agents/templates', token).then((d) => setTemplates(d.templates || [])).catch(() => {}) }, [token])
  useEffect(() => { api('/agents/llm-config', token).then(setLlmCurrent).catch(() => {}) }, [token])
  useEffect(() => { api('/agents/providers', token).then((d) => setProviders(d.providers || [])).catch(() => {}) }, [token])
  useEffect(() => { api('/usage', token).then(setUsage).catch(() => {}) }, [token])

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
    if (!window.confirm('确定终止并删除这个项目？（审计事件保留，项目从看板移除）')) return
    api(`/projects/${pid}/archive`, token, { method: 'POST' }).then(() => loadProjects()).catch((e) => setError(errMsg(e)))
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
  const audit = (detail?.audit || []).slice(-20).reverse()

  function decideApproval(aid, result) {
    api(`/approvals/${aid}/decision?result=${result}`, token, { method: 'POST' })
      .then(() => { if (selected) loadDetail(selected); showToast(result === 'approve' ? '✅ 已批准' : '已拒绝') })
      .catch((e) => setError(errMsg(e)))
  }

  function submitDeliverable(tid) {
    const fileRef = window.prompt('产出文件路径（如 docs/report.md）：', 'docs/')
    if (!fileRef) return
    const q = new URLSearchParams({ file_ref: fileRef, verdict: 'done' }).toString()
    api(`/tasks/${tid}/deliverables?${q}`, token, { method: 'POST' })
      .then(() => { if (selected) loadDetail(selected); showToast('📄 产出已提交 → 待复核') })
      .catch((e) => setError(errMsg(e)))
  }

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
          <input className="token" value={token} placeholder="Bearer token"
                 onChange={(e) => setToken(e.target.value)} title="API 访问 token" />
          <span className={`level-badge l${me?.level || '?'}`}>{me ? `L${me.level}` : '未认证'}</span>
          <button onClick={loadProjects}>刷新</button>
          <button className="primary" onClick={() => setShowFb(true)}>反馈</button>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

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

          <h3>项目</h3>
          {projects.length === 0 ? (
            <div className="empty">还没有项目，上面提个需求就开始了</div>
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
                      <button className="mini" title="终止/删除" onClick={(e) => { e.stopPropagation(); archiveProject(p.project_id) }}>×</button>
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
                <div className="welcome-title">从左边选一个项目，或上面提个新需求</div>
                <div className="welcome-sub">每个项目会展开：任务看板、审批队列、完整审计流</div>
              </div>
            ) : (
              <div className="proj-grid">
                {projects.map((p) => (
                  <div key={p.project_id} className="proj-card" onClick={() => loadDetail(p.project_id)}>
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
                          {t.status === 'in_progress' && (
                            <div className="t-actions">
                              <button className="mini" onClick={() => submitDeliverable(t.task_id)}>提交产出</button>
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
              {pendingApprovals.length === 0 ? <p className="muted">暂无待审批项</p> : (
                <div className="approvals">
                  {pendingApprovals.map((e, i) => (
                    <div className="ap-row" key={i}>
                      <span className="ap-type">待审批</span>
                      <span className="ap-scope">{e.payload?.scope || '流程变更'}</span>
                      {canL3 ? (
                        <span className="ap-actions">
                          <button className="mini ok" onClick={() => decideApproval(e.payload.approval_id, 'approve')}>✅ 批准</button>
                          <button className="mini danger" onClick={() => decideApproval(e.payload.approval_id, 'reject')}>✕ 拒绝</button>
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
