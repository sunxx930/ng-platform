import { useEffect, useState } from 'react'
import './App.css'

const STATUS_ORDER = ['todo', 'in_progress', 'in_review', 'pending_approval', 'blocked', 'completed', 'cancelled']
const STATUS_LABEL = {
  todo: '待办', in_progress: '进行中', in_review: '待复核',
  pending_approval: '待审批', blocked: '阻塞', completed: '完成', cancelled: '取消',
}

function api(path, token) {
  return fetch('/api' + path, {
    headers: { Authorization: `Bearer ${token}` },
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status}: ${r.statusText}`)
    return r.json()
  })
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
  const [goal, setGoal] = useState('')
  const [goalTitle, setGoalTitle] = useState('')
  const [showFb, setShowFb] = useState(false)
  const [fb, setFb] = useState({ content: '', contact: '', rating: null })

  function loadProjects() {
    api('/projects', token).then((d) => setProjects(d.projects || [])).catch((e) => setError(String(e)))
  }
  useEffect(loadProjects, [token])

  function loadDetail(pid) {
    setSelected(pid)
    setDetail(null)
    Promise.all([
      api(`/projects/${pid}/tasks`, token),
      api(`/projects/${pid}/audit`, token),
    ]).then(([t, a]) => setDetail({ tasks: t.tasks || [], audit: a.events || [] }))
      .catch((e) => setError(String(e)))
  }

  useEffect(() => { api('/agents', token).then((d) => setAgents(d.agents || [])).catch(() => {}) }, [token])
  useEffect(() => { api('/agents/templates', token).then((d) => setTemplates(d.templates || [])).catch(() => {}) }, [token])

  function addTemplate(id) {
    fetch(`/api/agents/templates/${id}/instantiate`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      .then((r) => r.json())
      .then(() => api('/agents', token).then((d) => setAgents(d.agents || [])))
      .catch((e) => setError(String(e)))
  }

  useEffect(() => { api('/agents/llm-config', token).then(setLlmCurrent).catch(() => {}) }, [token])
  useEffect(() => { api('/agents/providers', token).then((d) => setProviders(d.providers || [])).catch(() => {}) }, [token])

  function submitGoal() {
    const q = new URLSearchParams({ title: goalTitle || '新需求', goal }).toString()
    fetch(`/api/projects?${q}`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      .then((r) => r.json())
      .then((d) => {
        const g = new URLSearchParams({ body: goal, parse: 'true' }).toString()
        return fetch(`/api/projects/${d.project_id}/messages?${g}`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      })
      .then(() => { loadProjects(); setGoal(''); setGoalTitle(''); })
      .catch((e) => setError(String(e)))
  }

  function archiveProject(pid) {
    if (!window.confirm('确定终止并删除这个项目？（审计事件保留，项目从看板移除）')) return
    fetch(`/api/projects/${pid}/archive`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      .then((r) => { if (!r.ok) throw new Error('删除失败，可能 token 权限不足'); loadProjects() })
      .catch((e) => setError(String(e)))
  }

  const ngAgents = agents.filter((a) => (a.executor || 'builtin') === 'builtin' && a.name !== 'ng-assistant')

  function capName(n) {
    return String(n || '').split(/[-_\s]+/).filter(Boolean)
      .map((w) => (w.toLowerCase() === 'ng' ? 'NG' : w.charAt(0).toUpperCase() + w.slice(1)))
      .join(' ')
  }

  // 合并注册 agent + 模板为一个 Agent 列表；全部给＋（模板 instantiate，已注册 re-register 幂等）
  const regNames = new Set(ngAgents.map((a) => a.name))
  const combined = [
    ...ngAgents.map((a) => ({ key: 'r-' + a.name, name: capName(a.name), desc: a.capability || a.role || '', reg: a })),
    ...templates.filter((t) => !regNames.has(t.name) && !regNames.has(t.name_cn))
      .map((t) => ({ key: 't-' + t.id, id: t.id, name: t.name, desc: t.desc })),
  ].sort((a, b) => (a.name === 'NG助理' ? -1 : b.name === 'NG助理' ? 1 : 0))

  function addAgent(item) {
    if (item.id) return addTemplate(item.id)          // 模板 → instantiate
    const q = new URLSearchParams({ name: item.reg.name, capability: item.reg.capability || '', role: item.reg.role || '', executor: 'builtin' })
    return fetch(`/api/agents/register?${q}`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
      .then(() => api('/agents', token).then((d) => setAgents(d.agents || [])))
      .catch((e) => setError(String(e)))
  }

  function saveLlm() {
    fetch('/api/agents/llm-config', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(llm),
    }).then((r) => r.json())
      .then(() => { setLlmCurrent({ provider: llm.provider, model: llm.model, api_key_set: !!llm.api_key }); setLlm({ ...llm, api_key: '' }) })
      .catch((e) => setError(String(e)))
  }

  const tasksByStatus = (detail || {}).tasks || []
  const approvals = (detail?.audit || []).filter((e) => ['approval.requested', 'approval.decided'].includes(e.event_type))
  const audit = (detail?.audit || []).slice(-30).reverse()

  return (
    <div className="app">
      <header className="topbar">
        <h1><span className="accent">NG</span> AI Platform</h1>
        <div className="toolbar">
          <input
            className="token"
            value={token}
            placeholder="Bearer token"
            onChange={(e) => setToken(e.target.value)}
          />
          <button onClick={loadProjects}>刷新</button>
          <button onClick={() => setShowFb(true)}>反馈</button>
        </div>
      </header>

      {showFb && (
        <div className="modal">
          <div className="modal-box">
            <h3>提意见 / 反馈</h3>
            <textarea placeholder="告诉我们哪里好用、哪里不好用，或你的建议……" rows={4}
                      value={fb.content} onChange={(e) => setFb({ ...fb, content: e.target.value })} />
            <input placeholder="联系方式（可选，方便回访）" value={fb.contact}
                   onChange={(e) => setFb({ ...fb, contact: e.target.value })} />
            <div className="stars">
              {[1, 2, 3, 4, 5].map((s) => (
                <span key={s} className={fb.rating === s ? 'on' : ''} onClick={() => setFb({ ...fb, rating: s })}>★</span>
              ))}
            </div>
            <div className="modal-actions">
              <button onClick={() => setShowFb(false)}>取消</button>
              <button disabled={!fb.content.trim()} onClick={() => {
                fetch('/api/feedback', {
                  method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                  body: JSON.stringify(fb),
                }).then(() => { setShowFb(false); setFb({ content: '', contact: '', rating: null }) })
                  .catch((e) => setError(String(e)))
              }}>提交</button>
            </div>
          </div>
        </div>
      )}
      {error && <div className="error">{error}</div>}

      <div className="layout">
        <aside className="sidebar">
          <h3>算力配置</h3>
          <div className="llm">
            <select value={llm.provider}
                    onChange={(e) => {
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
            {llm.provider === 'ollama' && (
              <div className="p-sub">本地模型，无需 key，填模型名即可</div>
            )}
            <input type="password" placeholder="API key" value={llm.api_key}
                   onChange={(e) => setLlm({ ...llm, api_key: e.target.value })} />
            <input placeholder="模型（已按提供商预填，可改）" value={llm.model}
                   onChange={(e) => setLlm({ ...llm, model: e.target.value })} />
            {llm.base_url && (
              <input placeholder="base_url" value={llm.base_url} title="兼容端点的 base_url"
                     onChange={(e) => setLlm({ ...llm, base_url: e.target.value })} />
            )}
            <button onClick={saveLlm}>保存算力配置</button>
            {llmCurrent && (
              <div className="p-sub">当前: {llmCurrent.provider} · {llmCurrent.model || '-'} · key {llmCurrent.api_key_set ? '✓' : '✗'}</div>
            )}
          </div>

          <h3>项目（{projects.length}）</h3>
          <ul>
            {projects.map((p) => (
              <li key={p.project_id} className={p.project_id === selected ? 'active' : ''}
                  onClick={() => loadDetail(p.project_id)}>
                <div className="p-title">{p.title}</div>
                <div className="p-sub">{p.status} · {p.goal?.slice(0, 24)}
                  <button className="mini" title="终止/删除项目" onClick={(e) => { e.stopPropagation(); archiveProject(p.project_id) }}>删除</button>
                </div>
              </li>
            ))}
          </ul>
          <h3>Agent</h3>
          <ul className="agents">
            {combined.map((a) => (
              <li key={a.key}>
                <div className="a-name">{a.name}</div>
                <div className="a-desc">{a.desc}</div>
                <button className="mini" onClick={() => addAgent(a)}>＋</button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="content">
          <section className="goal-box">
            <h2>提需求</h2>
            <input className="goal-title" placeholder="需求标题（可选）" value={goalTitle}
                   onChange={(e) => setGoalTitle(e.target.value)} />
            <textarea placeholder="描述你的目标，平台自动拆任务、派 agent 干活……"
                      value={goal} onChange={(e) => setGoal(e.target.value)} rows={3} />
            <button onClick={submitGoal} disabled={!goal.trim()}>提交 → 自动组队开工</button>
          </section>

          {!selected && <div className="hint">← 选一个项目看任务看板</div>}

          {detail && (
            <>
              <h2>任务看板</h2>
              <div className="board">
                {STATUS_ORDER.map((st) => {
                  const ts = tasksByStatus.filter((t) => t.status === st)
                  if (!ts.length) return null
                  return (
                    <div className="col" key={st}>
                      <div className="col-head">{STATUS_LABEL[st]}（{ts.length}）</div>
                      {ts.map((t) => (
                        <div className="card" key={t.task_id}>
                          <div className="t-title">{t.title}</div>
                          <div className="t-meta">
                            {t.owner && <span>👤 {t.owner}</span>}
                            {t.reviewer && <span>🔍 {t.reviewer}</span>}
                            {t.has_deliverable && <span>📄 有产出</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )
                })}
              </div>

              <h2>审批队列（{approvals.length}）</h2>
              {approvals.length === 0 ? <p className="muted">无审批</p> : (
                <table className="tbl">
                  <tbody>
                    {approvals.map((e, i) => (
                      <tr key={i}>
                        <td>{e.event_type}</td>
                        <td>{e.payload?.scope || e.payload?.result}</td>
                        <td>{new Date(e.created_at_ts * 1000).toLocaleTimeString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <h2>审计（最近 30）</h2>
              <div className="audit">
                {audit.map((e, i) => (
                  <div className="ev" key={i}>
                    <span className="ev-type">{e.event_type}</span>
                    <span className="ev-pay">{JSON.stringify(e.payload || {}).slice(0, 80)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
