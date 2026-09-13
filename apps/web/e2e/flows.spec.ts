/**
 * User product flows for apps/web — one test per sub-feature in
 * .claude/skills/verify-faultline/features/web-ui.md. Keep the ids (F1…F11) and the feature ids in
 * the test titles in sync with that file: the verification runner reports `flows.<id>` from them.
 *
 * Every test starts from a fresh browser context (fresh identity, fresh theme, fresh layout) and
 * saves a full-page screenshot as an attachment named `screen`.
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test, type Page, type TestInfo } from '@playwright/test'

const SCREEN_DIR = process.env.PW_SCREEN_DIR ?? 'e2e-results/screens'
const OUTPUTS_DIR = process.env.PW_OUTPUTS_DIR ?? 'e2e-results/outputs'

function writeOutput(name: string, value: unknown) {
  mkdirSync(OUTPUTS_DIR, { recursive: true })
  writeFileSync(join(OUTPUTS_DIR, name), JSON.stringify(value, null, 2))
}
const LIVE = process.env.FAULTLINE_LIVE === '1'

async function shot(page: Page, info: TestInfo, name: string) {
  const path = `${SCREEN_DIR}/${name}.png`
  await page.screenshot({ path, fullPage: true })
  await info.attach('screen', { path, contentType: 'image/png' })
}

async function gotoHome(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('An agent harness where the environment fights back')
  // skeleton rows render first; wait for a real scenario row (it has a Run button)
  await expect(page.locator('table tbody tr').filter({ has: page.getByRole('button', { name: /^Run$|Starting/ }) }).first()).toBeVisible({ timeout: 60_000 })
}

const promptBox = (page: Page) => page.getByRole('textbox', { name: 'Prompt' })

/** Open the bottom-left user menu; the trigger also owns a tooltip, so give the popup a moment and retry once. */
async function openUserMenu(page: Page) {
  const trigger = page.locator('[data-slot=sidebar-footer] button').first()
  for (let attempt = 0; attempt < 3; attempt++) {
    await trigger.click()
    try {
      await expect(page.getByRole('menu')).toBeVisible({ timeout: 4_000 })
      return
    } catch {
      await page.keyboard.press('Escape')
      await page.waitForTimeout(300)
    }
  }
  await expect(page.getByRole('menu')).toBeVisible()
}
const rowFor = (page: Page, scenarioId: string) =>
  page.locator('table tbody tr').filter({ has: page.locator(`text=/^${scenarioId}$/`) })

test.describe('web product flows', () => {
  test('F1 web-landing: headline, what-goes-wrong list, try-it steps, centered composer, six-column scenario table, no health card', async ({ page }, info) => {
    await gotoHome(page)
    await expect(page.locator('[data-slot=kicker]')).toContainText('Theme 3: Systems & Reliability')
    await expect(page.getByRole('list', { name: 'What goes wrong' }).getByRole('listitem')).toHaveCount(4)
    await expect(page.getByRole('list', { name: 'Try it in 60 seconds' }).getByRole('listitem')).toHaveCount(4)
    await expect(promptBox(page)).toBeVisible()
    for (const h of ['Scenario', 'What goes wrong', 'Step budget', 'Failure', 'Run', 'Replay']) {
      await expect(page.locator('table thead')).toContainText(h)
    }
    // the catalogue's origin words reach the table: a staged deletion is not "simulated"
    await expect(rowFor(page, 'missing-config')).toContainText('staged: missing file')
    await expect(rowFor(page, 'worker-crash')).toContainText('real: worker crash')
    await expect.poll(async () => await page.locator('table tbody tr').filter({ has: page.getByRole('button', { name: /^Run$|Starting/ }) }).count()).toBeGreaterThanOrEqual(4)
    for (const id of ['lost-ack', 'locked-file', 'missing-config', 'gauntlet']) {
      const row = rowFor(page, id)
      await expect(row.getByRole('button', { name: /^Run$|Starting/ })).toBeVisible()
      await expect(row.getByRole('button', { name: /Replay/ })).toBeVisible()
    }
    await expect(page.getByText('Harness reachable')).toHaveCount(0)
    await shot(page, info, 'F1_landing')
  })

  test('F2 web-composer: slash menu filters, Enter picks and fills the task prompt', async ({ page }, info) => {
    await gotoHome(page)
    const box = promptBox(page)
    await box.click()
    await box.pressSequentially('/lost')
    const option = page.getByRole('option', { name: /lost-ack/ })
    await expect(option).toBeVisible()
    await shot(page, info, 'F2_slash_menu')
    await box.press('Enter')
    await expect(box).toHaveValue(/^Prepare release 0\.2\.0/)
    await expect(page.getByRole('option')).toHaveCount(0)
    await expect(page.getByText(/up to 20 steps · Enter to run/)).toBeVisible()
  })

  test('F5 web-replay: lost-ack replay plays in the browser with a scrubber and ends graded 100, story says recovered', async ({ page }, info) => {
    await gotoHome(page)
    await rowFor(page, 'lost-ack').getByRole('button', { name: /Replay/ }).click()
    await expect(page).toHaveURL(/\/replay\/lost-ack$/)
    await expect(page.locator('header')).toContainText('recorded replay')
    const controls = page.getByLabel('Replay controls')
    await expect(controls).toBeVisible()
    // Base UI slider: the label sits on the root, the thumb is a range input
    await expect(controls.locator('[data-slot=slider]')).toBeVisible()
    await expect(controls.locator('input[type=range]')).toHaveCount(1)
    await expect(controls.getByRole('button', { name: /Pause replay|Play replay/ })).toBeVisible()
    // play to the end: the transport becomes "Restart replay" and the story lands on the verdict
    await expect(controls.getByRole('button', { name: 'Restart replay' })).toBeVisible({ timeout: 90_000 })
    const story = page.getByRole('region', { name: 'What happened' })
    await expect(story).toContainText(/Verdict: 100\/100/i)
    await expect(story).toContainText(/recovered/)
    await expect(story).toContainText(/Score 100\/100/)
    await expect(page.locator('main')).toContainText(/verified_before_rewrite/)
    await shot(page, info, 'F5_replay_end')
  })

  test('F6 web-replay-story: the story bar explains each checkpoint in plain English with prev/next, and names the fault', async ({ page }, info) => {
    await page.goto('/replay/lost-ack')
    const controls = page.getByLabel('Replay controls')
    await expect(controls.getByRole('button', { name: 'Restart replay' })).toBeVisible({ timeout: 90_000 })
    const story = page.getByRole('region', { name: 'What happened' })
    await expect(story).toContainText(/Verdict/i)
    // v21: the bar has a fixed height so the transcript below never jumps between checkpoints;
    // a long narration scrolls inside its own box instead of growing the bar.
    const heights = new Set<number>()
    const barHeight = async () => Math.round((await story.boundingBox())!.height)
    heights.add(await barHeight())
    // walk backwards through the checkpoints until the one that reports the injected fault
    let found = false
    for (let i = 0; i < 14 && !found; i++) {
      await story.getByRole('button', { name: 'Previous step' }).click()
      heights.add(await barHeight())
      const text = (await story.textContent()) ?? ''
      if (/simulated: lost ack/i.test(text) || (/\bfault\b/i.test(text) && /CHANGELOG\.md|acknowledg/i.test(text))) found = true
    }
    expect(found).toBe(true)
    expect([...heights], 'story bar height changed between checkpoints (transcript would jump)').toHaveLength(1)
    expect([...heights][0]).toBeGreaterThanOrEqual(96)
    expect([...heights][0]).toBeLessThanOrEqual(120)
    const narration = story.locator('p')
    await expect(narration).toBeVisible()
    const overflow = await narration.evaluate((el) => getComputedStyle(el).overflowY)
    expect(overflow, 'narration must scroll inside the bar, not clip or grow it').toBe('auto')
    await shot(page, info, 'F6_story_fault_checkpoint')
    await story.getByRole('button', { name: 'Next step' }).click()
    await expect(story).not.toContainText(/^$/)
  })

  test('F7 web-workspace: resizable column, collapse toggle, virtualized logs', async ({ page }, info) => {
    await page.goto('/replay/lost-ack')
    const handle = page.locator('[data-slot=resizable-handle]')
    await expect(handle).toBeVisible()
    const workspace = page.locator('[data-slot=resizable-panel]#workspace')
    const before = (await workspace.boundingBox())!.width
    const box = (await handle.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + 300)
    await page.mouse.down()
    await page.mouse.move(box.x - 160, box.y + 300, { steps: 12 })
    await page.mouse.up()
    const after = (await workspace.boundingBox())!.width
    expect(after).toBeGreaterThan(before + 80)
    const saved = await page.evaluate(() =>
      Object.keys(localStorage).some((k) => k.includes('faultline.run-layout')),
    )
    expect(saved).toBe(true)
    const toggle = page.locator('header').getByRole('button', { name: 'Workspace' })
    await toggle.click()
    await expect.poll(async () => (await workspace.boundingBox())?.width ?? 0).toBeLessThan(2)
    await toggle.click()
    await expect.poll(async () => (await workspace.boundingBox())?.width ?? 0).toBeGreaterThan(200)
    // v22: a collapsed split is never carried into the next conversation/replay on a wide screen —
    // the panel is open again on arrival and the header toggle reads pressed.
    await toggle.click()
    await expect.poll(async () => (await workspace.boundingBox())?.width ?? 0).toBeLessThan(2)
    await page.goto('/replay/gauntlet')
    await expect(page.locator('[data-slot=resizable-panel]#workspace')).toBeVisible()
    await expect.poll(async () => (await page.locator('[data-slot=resizable-panel]#workspace').boundingBox())?.width ?? 0).toBeGreaterThan(200)
    await expect(page.locator('header').getByRole('button', { name: 'Workspace' })).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('tab', { name: 'Logs' }).click()
    const log = page.getByRole('log')
    await expect(log).toBeVisible()
    await expect.poll(async () => await log.locator('[data-index]').count()).toBeGreaterThan(0)
    const rendered = await log.locator('[data-index]').count()
    expect(rendered).toBeLessThanOrEqual(60)
    await shot(page, info, 'F7_workspace_logs')
  })

  test('F8 web-identity: minted id, cookie mirror, header on every request, fresh identity sees no history', async ({ page, context }, info) => {
    const seen: string[] = []
    page.on('request', (r) => {
      const h = r.headers()['x-faultline-user']
      if (h && /\/(conversations|me|runs)/.test(r.url())) seen.push(h)
    })
    await gotoHome(page)
    const id = await page.evaluate(() => localStorage.getItem('faultline.user_id'))
    expect(id).toMatch(/^u_[0-9a-f-]{36}$/)
    const cookies = await context.cookies()
    expect(cookies.find((c) => c.name === 'faultline_uid')?.value).toBe(id)
    await expect.poll(() => seen.length).toBeGreaterThan(0)
    expect(new Set(seen)).toEqual(new Set([id]))
    await expect(page.locator('[data-slot=sidebar]')).toContainText(/No conversations yet/)
    await openUserMenu(page)
    await expect(page.getByRole('menu')).toContainText(id!)
    await shot(page, info, 'F8_identity_menu')
  })

  test('F9 web-theme: light · dark · system from the user menu, persisted, URL override, no dark class in light', async ({ page }, info) => {
    await gotoHome(page)
    await openUserMenu(page)
    // The controls must be reachable by assistive technology: a menu *label* is aria-hidden in
    // Base UI, so interactive controls placed inside DropdownMenuLabel vanish from the a11y tree.
    const cssGroup = page.locator('[role=radiogroup][aria-label="Theme"]')
    await expect(cssGroup).toBeVisible()
    const hiddenAncestor = await cssGroup.evaluate((el) => !!el.parentElement?.closest('[aria-hidden="true"]'))
    expect(hiddenAncestor, 'Theme radiogroup is inside an aria-hidden ancestor (DropdownMenuLabel); render it outside the label').toBe(false)
    const group = page.getByRole('radiogroup', { name: 'Theme' })
    await expect(group).toBeVisible()
    await group.getByRole('radio', { name: 'Light theme' }).click()
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    expect(await page.evaluate(() => localStorage.getItem('faultline.theme'))).toBe('light')
    await shot(page, info, 'F9_theme_light')
    await page.reload()
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    await page.goto('/?theme=dark')
    await expect(page.locator('html')).toHaveClass(/dark/)
    expect(await page.evaluate(() => localStorage.getItem('faultline.theme'))).toBe('dark')
    await openUserMenu(page)
    await page.getByRole('radiogroup', { name: 'Theme' }).getByRole('radio', { name: 'System theme' }).click()
    expect(await page.evaluate(() => localStorage.getItem('faultline.theme'))).toBe('system')
  })

  test('F10 web-failure-modes: unknown run shows a clear error, never a provisioning spinner', async ({ page }, info) => {
    await page.goto('/runs/r_doesnotexist')
    await expect(page.getByText(/Could not load this run/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/Provisioning the sandbox/)).toHaveCount(0)
    await shot(page, info, 'F10_unknown_run')
    // status pills come from structured fields only: in the lost-ack replay the write whose
    // acknowledgement was withheld is "no ack" in the unknown tone, never a red failure.
    await page.goto('/replay/lost-ack')
    await expect(page.getByLabel('Replay controls').getByRole('button', { name: 'Restart replay' })).toBeVisible({ timeout: 90_000 })
    const step = page.locator('section[aria-label^="Step "]', { hasText: /unknown/ }).first()
    await expect(step).toBeVisible()
    const collapsed = step.locator('[aria-expanded="false"]').first()
    if (await collapsed.count()) await collapsed.click()
    const pill = step.locator('[data-status="unknown"]').first()
    await expect(pill).toContainText(/no ack/)
    await expect(step.locator('[data-status="error"]')).toHaveCount(0)
    await expect(page.locator('[data-status="ok"]').first()).toBeVisible()
    await shot(page, info, 'F10_no_ack_pill')
  })

  test('F11 web-sidebar: collapses to icons and back', async ({ page }, info) => {
    await gotoHome(page)
    const sidebar = page.locator('[data-slot=sidebar]').first()
    await expect(sidebar).toHaveAttribute('data-state', 'expanded')
    await page.locator('[data-slot=sidebar-trigger]').click()
    await expect(sidebar).toHaveAttribute('data-state', 'collapsed')
    await shot(page, info, 'F11_sidebar_collapsed')
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+b' : 'Control+b')
    await expect(sidebar).toHaveAttribute('data-state', 'expanded')
  })

  // Real-failure specimens from the backend's interruptions proof (features/interruptions.md), owned
  // by the CLI identity below. The persisted (SQLite) projection is the default view of every
  // finished run after a page load, so it must keep "unknown" / "not executed" distinct from a
  // failed call there too (docs/web-review-findings.md §2) — never blame the agent for a dead worker
  // or a dead sandbox.
  const SPECIMEN_USER = 'u_00000000-0000-4000-8000-000000000000'
  const SPECIMENS = {
    workerCrash: { conversation: 'c_1a0982afb68bab29b5bc380', run: 'r_2991dd9a680a' },
    sandboxLoss: { conversation: 'c_1a0982c9806c48810779d36', run: 'r_77368c6c998d' },
  }

  test('F12 web-persisted-provenance: a resumed worker crash and a lost sandbox keep unknown / not-executed status after reload, never a red failure', async ({ page, request }, info) => {
    const cfg = (await (await request.get('/config.json')).json()) as { harnessUrl: string }
    const probe = await request.get(`${cfg.harnessUrl}/conversations/${SPECIMENS.workerCrash.conversation}`, { headers: { 'X-Faultline-User': SPECIMEN_USER } })
    test.skip(probe.status() !== 200, `specimen conversation answered ${probe.status()} (Store restoring after a deploy — B6 — or the specimen moved); not a UI verdict`)
    await page.addInitScript((id) => {
      try {
        localStorage.setItem('faultline.user_id', id)
      } catch {
        /* ignore */
      }
    }, SPECIMEN_USER)

    const expandAll = async () => {
      for (let i = 0; i < 40; i++) {
        const t = page.locator('main section[aria-label^="Step "] [aria-expanded="false"]').first()
        if ((await t.count()) === 0) break
        await t.click()
      }
    }

    // (a) worker crash: the write that was in flight when the worker died is "unknown", the run is graded.
    await page.goto(`/conversations/${SPECIMENS.workerCrash.conversation}?run=${SPECIMENS.workerCrash.run}`)
    await expect(page.locator('header').getByText(/sqlite/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/Step 1 of/).first()).toBeVisible()
    await expandAll()
    await expect(page.locator('[data-status]').first()).toBeVisible()
    await shot(page, info, 'F12_worker_crash_persisted')
    // soft: keep going so one run reports both specimens
    await expect.soft(page.locator('main [data-status="error"]'), 'the interrupted write (EHARNESS) must not render as a failed call on the persisted view').toHaveCount(0)
    await expect.soft(page.locator('main [data-status="unknown"]').first(), 'the interrupted write must be "unknown" on the persisted view').toBeVisible()
    await expect(page.locator('main')).toContainText(/verified_before_rewrite|Passed|Score/i)

    // (b) sandbox loss: the read that hit the dead sandbox is not the agent's failure; the run is interrupted and not graded.
    await page.goto(`/conversations/${SPECIMENS.sandboxLoss.conversation}?run=${SPECIMENS.sandboxLoss.run}`)
    await expect(page.locator('header').getByText(/sqlite/)).toBeVisible({ timeout: 60_000 })
    await expect(page.locator('header').getByText(/^interrupted$/)).toBeVisible()
    await expandAll()
    await expect(page.locator('[data-slot="not-graded"]')).toBeVisible()
    await shot(page, info, 'F12_sandbox_loss_persisted')
    await expect.soft(page.locator('main [data-status="error"]'), 'ESANDBOX on a lost sandbox must not render as a failed call').toHaveCount(0)
    await expect.soft(page.locator('main [data-status="unknown"], main [data-status="not-executed"]').first(), 'the ESANDBOX read must be unknown / not executed').toBeVisible()
  })

  test.describe('live conversation (FAULTLINE_LIVE=1)', () => {
    test.describe.configure({ mode: 'serial' })
    test.skip(!LIVE, 'set FAULTLINE_LIVE=1 (verify_web.py --live) to spend one Haiku episode')

    let liveUrl: string | null = null
    let runId: string | null = null
    let conversationId: string | null = null
    let liveUserId: string | null = null

    /** Each test gets a fresh browser context (fresh identity); later steps must act as the user who started the run. */
    async function adoptIdentity(page: Page) {
      await page.addInitScript((id) => {
        try {
          localStorage.setItem('faultline.user_id', id)
        } catch {
          /* ignore */
        }
      }, liveUserId!)
    }

    test('F3 web-live-run: Run from the table → conversation → live SSE → finished → persisted (sqlite) chip', async ({ page }, info) => {
      test.setTimeout(6 * 60_000)
      await gotoHome(page)
      await rowFor(page, 'lost-ack').getByRole('button', { name: /^Run$/ }).click()
      await expect(page).toHaveURL(/\/conversations\/c_[a-z0-9]+\?run=r_[a-z0-9]+/, { timeout: 60_000 })
      conversationId = new URL(page.url()).pathname.split('/').pop()!
      runId = new URL(page.url()).searchParams.get('run')!
      liveUrl = page.url()
      writeOutput('live.json', { run_id: runId, conversation_id: conversationId, started_at: new Date().toISOString(), url: liveUrl })
      await expect(page.locator('[data-slot=sidebar]')).toContainText(/Release 0\.2\.0/)
      await expect(page.locator('header')).toContainText(/live · (sse|poll)/, { timeout: 60_000 })
      await shot(page, info, 'F3_live_streaming')
      // the status badge's own text is exactly the run status (header textContent concatenates nodes without spaces)
      const statusBadge = page.locator('header').getByText(/^(ok|unevaluated|interrupted|truncated|error)$/)
      await expect(statusBadge).toBeVisible({ timeout: 4 * 60_000 })
      const finalStatus = (await statusBadge.textContent())?.trim()
      await expect(page.locator('header').getByText(/sqlite/)).toBeVisible({ timeout: 60_000 })
      await shot(page, info, 'F3_finished_sqlite')
      const userId = await page.evaluate(() => localStorage.getItem('faultline.user_id'))
      liveUserId = userId
      writeOutput('live.json', { run_id: runId, conversation_id: conversationId, user_id: userId, final_status: finalStatus, url: liveUrl, finished_at: new Date().toISOString() })
    })

    test('F4 web-persisted: hard reload restores the transcript from GET /conversations/{id}; other identities get 404', async ({ page, request }, info) => {
      test.skip(!liveUrl || !liveUserId, 'F3 did not produce a conversation')
      await adoptIdentity(page)
      await page.goto(liveUrl!)
      await expect(page.locator('header').getByText(/sqlite/)).toBeVisible({ timeout: 60_000 })
      await expect(page.getByText(/Prepare release 0\.2\.0/).first()).toBeVisible()
      await expect(page.getByText(/Step 1 of/).first()).toBeVisible()
      await expect(page.getByText(/another run in this conversation/)).toBeVisible()
      await shot(page, info, 'F4_persisted_after_reload')
      const userId = await page.evaluate(() => localStorage.getItem('faultline.user_id'))
      const cfg = (await (await request.get('/config.json')).json()) as { harnessUrl: string }
      const mine = await request.get(`${cfg.harnessUrl}/conversations/${conversationId}`, { headers: { 'X-Faultline-User': userId! } })
      expect(mine.ok()).toBe(true)
      const body = await mine.json()
      writeOutput('conversation.json', body)
      writeOutput('live.json', { run_id: runId, conversation_id: conversationId, user_id: userId, harness_url: cfg.harnessUrl, url: liveUrl, verified_at: new Date().toISOString() })
      await info.attach('conversation.json', { body: JSON.stringify(body, null, 2), contentType: 'application/json' })
      expect(Array.isArray(body.messages) && body.messages.length).toBeTruthy()
      const other = await request.get(`${cfg.harnessUrl}/conversations/${conversationId}`, { headers: { 'X-Faultline-User': 'u_22222222-2222-4222-8222-222222222222' } })
      expect(other.status()).toBe(404)
    })

    test('F4b web-persisted-score: the persisted transcript shows the grader verdict (score card)', async ({ page }) => {
      test.skip(!liveUrl || !liveUserId, 'F3 did not produce a conversation')
      // Fixed in faultline-web v20 (2026-09-12 20:14 EDT): the persisted transcript shows the run's
      // grade, or a "Not graded" callout (data-slot="not-graded") when it could not be evaluated.
      await adoptIdentity(page)
      await page.goto(liveUrl!)
      await expect(page.locator('header').getByText(/sqlite/)).toBeVisible({ timeout: 60_000 })
      await expect(page.locator('main')).toContainText(/verified_before_rewrite|Passed|Failed|Not graded/i, { timeout: 20_000 })
    })
  })
})
