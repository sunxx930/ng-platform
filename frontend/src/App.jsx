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

  const tasksByStatus = (detail || {}).tasks || []
  const approvals = (detail?.audit || []).filter((e) => ['approval.requested', 'approval.decided'].includes(e.event_type))
  const audit = (detail?.audit || []).slice(-30).reverse()

  return (
    <div className="app">
      <header className="topbar">
        <h1>NG AI Platform</h1>
        <div className="toolbar">
          <input
            className="token"
            value={token}
            placeholder="Bearer token"
            onChange={(e) => setToken(e.target.value)}
          />
          <button onClick={loadProjects}>刷新</button>
        </div>
      </header>
      {error && <div className="error">{error}</div>}

      <div className="layout">
        <aside className="sidebar">
          <h3>项目（{projects.length}）</h3>
          <ul>
            {projects.map((p) => (
              <li key={p.project_id} className={p.project_id === selected ? 'active' : ''}
                  onClick={() => loadDetail(p.project_id)}>
                <div className="p-title">{p.title}</div>
                <div className="p-sub">{p.status} · {p.goal?.slice(0, 24)}</div>
              </li>
            ))}
          </ul>
          <h3>Agent（{agents.length}）</h3>
          <ul className="agents">
            {agents.map((a) => (
              <li key={a.name}><b>{a.name}</b> <span className="cap">{a.capability}</span></li>
            ))}
          </ul>
          <h3>preagent 模板库（{templates.length}）</h3>
          <ul className="agents">
            {templates.map((t) => (
              <li key={t.id}>
                <span><b>{t.name}</b> <span className="cap">{t.capability.slice(0, 20)}</span></span>
                <button className="mini" onClick={() => addTemplate(t.id)}>＋</button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="content">
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
