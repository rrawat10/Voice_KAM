import { test, expect } from '@playwright/test';

test('mock data, isolated session and refresh', async ({ page, browser }) => {
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Let’s talk business.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start conversation' })).toBeDisabled();
  await page.getByRole('button', { name: 'Use mock data' }).click();
  await expect(page.getByRole('cell', { name: '₹1,35,000', exact: true })).toBeVisible();
  await expect(page.getByLabel('Are these comparable time periods?')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/desktop.png', fullPage: true });
  const separate = await browser.newContext();
  const other = await separate.newPage(); await other.goto('/');
  await other.getByRole('button', { name: 'Data preview' }).click();
  await expect(other.getByRole('heading', { name: 'A clear starting point' })).toBeVisible();
  await separate.close();
  await page.reload();
  await page.getByRole('button', { name: 'Data preview' }).click();
  await expect(page.getByRole('cell', { name: '₹1,35,000', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'End & clear session' })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('template upload, bad workbook error and responsive layout', async ({ page, request }) => {
  await page.goto('/');
  const template = await request.get('/api/template');
  await page.locator('input[type=file]').setInputFiles({ name: 'seller-data.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: await template.body() });
  await expect(page.getByText('Uploaded spreadsheet', { exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: '₹1,35,000', exact: true })).toBeVisible();
  await page.locator('input[type=file]').setInputFiles({ name: 'broken.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('not excel') });
  await expect(page.getByRole('alert')).toContainText('could not be read');
  await expect(page.getByRole('cell', { name: '₹1,35,000', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Dismiss error' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/mobile.png', fullPage: true });
});

test('SSE updates UI from accepted state', async ({ page }) => {
  await page.goto('/');
  await page.route('**/api/events', async route => {
    const session = await (await page.request.get('/api/session')).json();
    session.facts = { supplier: { key: 'supplier', text: 'Supplier is late', quote: 'My supplier is late', turn: 1 } };
    session.summary = 'The seller reports supplier delays. A confirmed delivery date remains unknown.';
    session.history = [{ role: 'user', text: 'My supplier is late', turn: 1 }, { role: 'assistant', text: 'Do you have a confirmed delivery date?', turn: 1 }];
    session.plan = { unknowns: ['Delivery date remains unknown'], findings: [], recommendations: [] };
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ type: 'snapshot', data: session })}\n\n` });
  });
  await page.getByRole('button', { name: 'Use mock data' }).click();
  await page.getByRole('button', { name: /Live understanding/ }).click();
  await expect(page.locator('.fact-list')).toContainText('Supplier is late');
  await expect(page.getByText('Do you have a confirmed delivery date?', { exact: true })).toBeVisible();
  await page.getByText('Conversation summary', { exact: true }).click();
  await expect(page.getByText('The seller reports supplier delays. A confirmed delivery date remains unknown.', { exact: true })).toBeVisible();
});

test('returning visitor sees an expired-session notice', async ({ page }) => {
  await page.route('**/api/session', route => route.fulfill({ status: 410, contentType: 'application/json', body: JSON.stringify({ detail: 'Your previous session expired or was cleared. Load your data to start again.' }) }));
  await page.goto('/');
  await expect(page.getByRole('status')).toContainText('previous session expired');
  await expect(page.getByRole('button', { name: 'Start conversation' })).toBeDisabled();
  await page.unroute('**/api/session');
  await page.getByRole('button', { name: 'Use mock data' }).click();
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(page.getByRole('cell', { name: '₹1,35,000', exact: true })).toBeVisible();
});

test('voice controls and PCM streaming work with a mocked provider', async ({ page }) => {
  let frames = 0;
  await page.route('**/api/health', route => route.fulfill({ json: { ok: true, voice_configured: true, storage: 'Test memory', max_call_seconds: 300 } }));
  await page.routeWebSocket('**/api/voice', ws => {
    ws.send(JSON.stringify({ type: 'ready' }));
    ws.onMessage(message => {
      if (typeof message !== 'string') frames++;
    });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Use mock data' }).click();
  await page.getByRole('button', { name: 'Start conversation' }).click();
  await expect(page.getByRole('button', { name: 'Pause conversation' })).toBeVisible();
  await expect.poll(() => frames).toBeGreaterThan(3);
  await page.getByRole('button', { name: 'Mute microphone' }).click();
  await expect(page.getByRole('button', { name: 'Unmute microphone' })).toBeVisible();
  await page.getByRole('button', { name: 'Pause conversation' }).click();
  await expect(page.getByRole('button', { name: 'Reconnect with Mira' })).toBeVisible();
});
