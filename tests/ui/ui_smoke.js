// 前端点击级 UI 冒烟（Playwright）—— v1.2.0 起纳入。
// 覆盖：登录/注册可用、品牌 ™、版本徽标、更新按钮。
// 前置：dev 三件套已起（后端 :8020 + vite :5173）。运行：node tests/ui/ui_smoke.js
const fs = require('fs')
const path = require('path')

function loadPlaywright() {
  try { return require('playwright') } catch (e) { /* fallback to npx cache */ }
  const cands = [
    process.env.PW_PATH,
    '/Users/sunxx930/.npm/_npx/e41f203b7505f1fb/node_modules/playwright',
  ].filter(Boolean)
  for (const c of cands) {
    if (fs.existsSync(path.join(c, 'index.js'))) return require(c)
  }
  throw new Error('找不到 playwright，请 npm i -D playwright 或设 PW_PATH')
}
const { chromium } = loadPlaywright()
const BASE = process.env.UI_BASE || 'http://localhost:5173'
const uname = 'ui' + Date.now().toString(36).slice(-6)

;(async () => {
  const fail = []
  const assert = (c, m) => { console.log(`${c ? '✓' : '✗'} ${m}`); if (!c) fail.push(m) }
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PW_EXEC || undefined,
    args: ['--no-sandbox'],
  })
  const page = await browser.newPage()
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 })
    assert((await page.getByTestId('auth-username').count()) === 1, '登录页出现')
    const authBrand = await page.locator('.brand-name').first().textContent().catch(() => '')
    assert((authBrand || '').includes('™'), `登录页品牌带 ™（${authBrand}）`)
    await page.getByTestId('auth-to-register').click()   // 切到注册
    await page.getByTestId('auth-username').fill(uname)
    await page.getByTestId('auth-password').fill('secret123')
    await page.getByTestId('auth-submit').click()
    let ok = false
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(500)
      if ((await page.getByTestId('logout').count()) > 0) { ok = true; break }
    }
    if (!ok) {
      const err = await page.getByTestId('auth-error').textContent().catch(() => '(无)')
      const body = await page.locator('body').innerText().catch(() => '')
      console.log('  注册失败，auth-error=', err, '\n  body片段=', body.slice(0, 220))
    }
    assert(ok, '注册并进入成功')
    await page.waitForTimeout(500)
    const top = await page.locator('.topbar').textContent().catch(() => '')
    assert((top || '').includes('™'), '顶栏品牌带 ™')
    const ver = await page.getByTestId('app-version').textContent().catch(() => null)
    assert(ver === 'v1.2.0', `版本徽标 = v1.2.0（得到 ${ver}）`)
    assert((await page.getByTestId('check-update').count()) === 1, '「↻ 更新」按钮存在')
    console.log(`\nUI 冒烟（用户 ${uname}）: ${fail.length ? '失败 → ' + fail.join('; ') : '全过 ✅'}`)
    process.exit(fail.length ? 1 : 0)
  } finally {
    await browser.close().catch(() => {})
  }
})().catch((e) => { console.error('脚本异常:', e.message); process.exit(2) })
