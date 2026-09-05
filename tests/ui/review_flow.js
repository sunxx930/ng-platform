// 复核流程点击级 UI 回归（Playwright）—— v1.2.0 复核 UI（权限按钮/意见门/自动复算）。
// 前置：后端 :8020 + vite :5173 已起。运行：node tests/ui/review_flow.js
// 浏览器：默认 playwright 内置；或 PW_EXEC=<路径> 指向本机缓存的 chrome-headless-shell。
const fs = require('fs')
const path = require('path')

function loadPlaywright() {
  try { return require('playwright') } catch (e) { /* fallback */ }
  const cands = [process.env.PW_PATH,
    '/Users/sunxx930/.npm/_npx/e41f203b7505f1fb/node_modules/playwright'].filter(Boolean)
  for (const c of cands) if (fs.existsSync(path.join(c, 'index.js'))) return require(c)
  throw new Error('找不到 playwright')
}
const { chromium } = loadPlaywright()
const BASE = process.env.UI_BASE || 'http://localhost:5173'
const uname = 'rv' + Date.now().toString(36).slice(-6)

async function seedTask(page, title) {
  // 用会话 token 经 /api 建项目+数值任务+交付物(in_review + pending review)
  const s = await page.evaluate(() => JSON.parse(localStorage.getItem('ng_session') || 'null'))
  const h = { Authorization: 'Bearer ' + s.token, 'Content-Type': 'application/json' }
  const pid = await page.evaluate(async (h) => {
    const b = await fetch('/api/projects?title=t&goal=g', { method: 'POST', headers: h }).then(r => r.json())
    return b.project_id
  }, h)
  // create task with query params
  const tid = await page.evaluate(async ({ pid, title, h }) => {
    const q = new URLSearchParams({ title, description: '输入 [1,2,3] 均值' })
    const r = await fetch(`/api/projects/${pid}/tasks?${q}`, { method: 'POST', headers: h })
    return (await r.json()).task_id
  }, { pid, title, h })
  const act = async (url) => page.evaluate(async ({ url, h }) => {
    await fetch(url, { method: 'PATCH', headers: h }).then((r) => r.json())
  }, { url: `/api/tasks/${tid}/state?to=in_progress`, h })
  await act()
  await page.evaluate(async ({ tid, h }) => {
    await fetch(`/api/tasks/${tid}/deliverables?file_ref=artifacts/rv.md&verdict=done&summary=s`, { method: 'POST', headers: h })
  }, { tid, h })
  return { pid, tid }
}

;(async () => {
  const fail = []
  const assert = (c, m) => { console.log(`${c ? '✓' : '✗'} ${m}`); if (!c) fail.push(m) }
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_EXEC || undefined, args: ['--no-sandbox'] })
  const page = await browser.newPage()
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 })
    await page.getByTestId('auth-to-register').click()
    await page.getByTestId('auth-username').fill(uname)
    await page.getByTestId('auth-password').fill('secret123')
    await page.getByTestId('auth-submit').click()
    for (let i = 0; i < 24 && (await page.getByTestId('logout').count()) === 0; i++) await page.waitForTimeout(500)
    assert((await page.getByTestId('logout').count()) > 0, '注册并进入')
    // seed 数值任务项目
    await seedTask(page, '计算均值胜率')
    await page.getByTestId('logout').waitFor({ timeout: 5000 }).catch(() => {})
    await page.reload({ waitUntil: 'domcontentloaded' })
    for (let i = 0; i < 24 && (await page.getByTestId('project-card').count()) === 0; i++) await page.waitForTimeout(500)
    assert((await page.getByTestId('project-card').count()) > 0, '看板出现项目卡')
    await page.getByTestId('project-card').first().click()
    // 等待复核行渲染（含数值复算跑完）
    let found = false
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(500)
      if ((await page.getByTestId('review-changes').count()) > 0) { found = true; break }
    }
    assert(found, '待复核行出现（含复核按钮）')
    assert((await page.getByTestId('review-pass').count()) > 0, '✅ 通过按钮可见(owner 可审)')
    assert((await page.getByTestId('review-numeric-note').count()) > 0, '数值任务提示可见')
    // 意见门：不打意见点"需修改" → 报错
    await page.getByTestId('review-changes').first().click()
    let errShown = false
    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(300)
      const t = await page.getByTestId('error-banner').textContent().catch(() => '')
      if (t && t.includes('意见')) { errShown = true; break }
    }
    assert(errShown, '未填意见点打回 → 前端拦截提示')
    // 填意见 → 打回成功
    await page.getByTestId('review-opinion').first().fill('把均值按输入重算再交')
    await page.getByTestId('review-changes').first().click()
    let reworked = false
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(500)
      if ((await page.getByTestId('review-changes').count()) === 0) { reworked = true; break }
    }
    assert(reworked, '打回成功(带意见) → 复核行消失/退回返工')
    console.log(`\n复核流程 UI 回归（用户 ${uname}）: ${fail.length ? '失败 → ' + fail.join('; ') : '全过 ✅'}`)
    process.exit(fail.length ? 1 : 0)
  } finally { await browser.close().catch(() => {}) }
})().catch((e) => { console.error('脚本异常:', e.message); process.exit(2) })
