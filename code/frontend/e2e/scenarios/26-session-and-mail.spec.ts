import { expect, test, type BrowserContext, type Page } from '@playwright/test'

import { API_BASE, installSessionCookies } from '../helpers/api'
import { dismissOnboardingIfVisible } from '../helpers/onboarding'

test.describe('Scenario 26 — session stays, one refresh, resend wait', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  test('signup link, reload, single refresh, logout', async ({ page }) => {
    const email = `session.e2e.${Date.now()}@example.com`
    const password = 'password12'
    let refreshes = 0
    await page.route('**/api/auth/refresh', async (route) => {
      refreshes += 1
      await route.continue()
    })

    await page.goto('/')
    await page.getByTestId('app-shell-register').click()
    await page.getByTestId('auth-email').fill(email)
    await page.getByTestId('auth-password').fill(password)
    await page.getByTestId('auth-password-confirm').fill(password)
    await page.getByTestId('auth-submit').click()
    await expect(page.getByTestId('register-sent-message')).toContainText('메일은 보내지 않았습니다')
    await page.getByTestId('register-dev-link').click()
    await expect(page.getByTestId('verify-email-page')).toContainText('이메일 인증이 완료되었습니다')
    await page.goto('/')
    await expect(page.getByTestId('app-shell-user')).toContainText(email.split('@')[0])
    await expect(page.getByTestId('email-verify-banner')).toHaveCount(0)
    expect(await page.evaluate(() => localStorage.getItem('flowshorts_access_token'))).toBeNull()
    expect(await page.evaluate(() => localStorage.getItem('flowshorts_refresh_token'))).toBeNull()

    await page.reload()
    await expect(page.getByTestId('app-shell-user')).toContainText(email.split('@')[0])

    const names = (await page.context().cookies()).map((item) => item.name)
    expect(names).toContain('access_token')
    expect(names).toContain('refresh_token')
    expect(names).toContain('signed_in')

    const beforeExpire = refreshes
    let forceMe401 = 1
    await page.route('**/api/auth/me', async (route) => {
      if (forceMe401 > 0 && route.request().method() === 'GET') {
        forceMe401 -= 1
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Unauthorized' }),
        })
        return
      }
      await route.continue()
    })
    await page.evaluate(async () => {
      const client = await import('/src/lib/apiClient.ts')
      await Promise.all([client.apiFetch('/auth/me'), client.apiFetch('/jobs')])
    })
    await page.unroute('**/api/auth/me')
    expect(refreshes).toBe(beforeExpire + 1)

    await page.getByTestId('app-shell-user').click()
    await page.getByTestId('app-shell-logout').click()
    await expect(page.getByTestId('app-shell-login')).toBeVisible()
  })

  test('verification resend waits 60 seconds', async ({ page, request }) => {
    const email = `resend.e2e.${Date.now()}@example.com`
    await page.goto('/')
    await page.getByTestId('app-shell-register').click()
    await page.getByTestId('auth-email').fill(email)
    await page.getByTestId('auth-password').fill('password12')
    await page.getByTestId('auth-password-confirm').fill('password12')
    await page.getByTestId('auth-submit').click()
    await page.getByRole('button', { name: '시작하기' }).click()
    await page.getByTestId('verify-resend').click()
    await expect(page.getByTestId('verify-resend')).toBeDisabled()
    await expect(page.getByTestId('verify-resend')).toContainText('초')
    const accessToken = (await page.context().cookies()).find((item) => item.name === 'access_token')?.value
    expect(accessToken).toBeTruthy()
    const again = await request.post('/api/auth/verify-email/request', {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    expect(again.status()).toBe(429)
  })
})

test.describe('Scenario 26 — two tabs share the cookie session', () => {
  test('both tabs stay signed in', async ({ browser }) => {
    const context = await browser.newContext({ storageState: 'e2e/.auth/user.json' })
    const first = await context.newPage()
    const second = await context.newPage()
    await first.goto('/')
    await second.goto('/')
    await expect(first.getByTestId('app-shell-user')).toBeVisible()
    await expect(second.getByTestId('app-shell-user')).toBeVisible()
    await context.close()
  })
})

test.describe('Scenario 26 — logout is per device', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  test('logout keeps the other device signed in; "모든 기기" ends them all', async ({ browser, request }) => {
    // Its own account: logout-all must not end the demo session the other scenarios share.
    const email = `devices.${Date.now()}@example.com`
    const password = 'devices12345'
    const registered = await request.post(`${API_BASE}/auth/register`, { data: { email, password } })
    expect(registered.ok(), await registered.text()).toBeTruthy()

    const contexts: BrowserContext[] = []
    const signInOnNewDevice = async (): Promise<Page> => {
      const login = await request.post(`${API_BASE}/auth/login`, { data: { email, password } })
      expect(login.ok(), await login.text()).toBeTruthy()
      const context = await browser.newContext()
      contexts.push(context)
      const page = await context.newPage()
      await page.goto('/')
      await installSessionCookies(page, (await login.json()).tokens)
      await page.reload()
      await dismissOnboardingIfVisible(page)
      await expect(page.getByTestId('app-shell-user')).toBeVisible()
      return page
    }

    const phone = await signInOnNewDevice()
    const laptop = await signInOnNewDevice()
    await phone.getByTestId('app-shell-user').click()
    await phone.getByTestId('app-shell-logout').click()
    await expect(phone.getByTestId('app-shell-login')).toBeVisible()
    await laptop.reload()
    await expect(laptop.getByTestId('app-shell-user')).toBeVisible()

    const tablet = await signInOnNewDevice()
    await laptop.goto('/account')
    await dismissOnboardingIfVisible(laptop)
    await laptop.getByTestId('account-logout-all').click()
    await expect(laptop.getByTestId('app-shell-login')).toBeVisible()
    await tablet.reload()
    await expect(tablet.getByTestId('app-shell-login')).toBeVisible()

    for (const context of contexts) await context.close()
  })
})
