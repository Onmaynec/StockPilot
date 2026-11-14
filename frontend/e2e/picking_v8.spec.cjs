const { test, expect } = require('@playwright/test');

async function login(page, role) {
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Электронная почта' })
    .fill(role + '@stockpilot.local');
  await page.locator('input[type="password"]')
    .fill(process.env.DEMO_PASSWORD || 'DemoStock2026!');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByText('Открытые заказы')).toBeVisible();
}

async function showPicks(page) {
  if ((await page.viewportSize())?.width <= 850) {
    await page.getByRole('button', { name: 'Открыть меню' }).click();
  }
  await page.locator('.side-nav')
    .getByRole('button', { name: 'Задания отбора' }).click();
  await expect(page.locator('.page-title h1')).toHaveText('Задания отбора');
}

async function signOut(page) {
  await page.locator('button[title="Выйти"]').click();
}

test('v0.8: supervisor assignment and personal picking queue', async ({ page }) => {
  test.setTimeout(90_000);
  await login(page, 'manager');
  const token = await page.evaluate(() => sessionStorage.getItem('stockpilot_token'));
  const headers = { Authorization: 'Bearer ' + token };
  const product = await page.request.get('/api/v1/products?q=SKU-00001', { headers });
  const sku = (await product.json()).items[0];
  const wh = (await (await page.request.get('/api/v1/warehouses', { headers })).json())[0];
  const name = 'Mobile08-' + Date.now();
  const orderResponse = await page.request.post('/api/v1/orders', {
    headers, data: { customer: name, items: [
      { productId: sku.id, warehouseId: wh.id, qty: 2 },
      { productId: sku.id, warehouseId: wh.id, qty: 1 }
    ] }
  });
  expect(orderResponse.status()).toBe(201);
  const orderId = (await orderResponse.json()).id;
  const reserve = await page.request.post('/api/v1/orders/' + orderId + '/reserve', {
    headers: { ...headers, 'Idempotency-Key': 'mobile08-reserve-' + orderId },
    data: { ttlMinutes: 60 }
  });
  expect(reserve.status()).toBe(200);
  await signOut(page);

  await login(page, 'supervisor');
  await showPicks(page);
  const cards = page.locator('.pick-card').filter({ hasText: name });
  await expect(cards).toHaveCount(2);
  await expect(cards.first().getByRole('option', { name: 'Кладовщик 12' })).toBeAttached();
  for (let index = 0; index < 2; index++) {
    const response = page.waitForResponse(r => r.url().includes('/assign') &&
      r.request().method() === 'POST');
    await cards.nth(index).getByRole('combobox').selectOption({ label: 'Кладовщик 12' });
    expect((await response).status()).toBe(200);
  }
  await signOut(page);

  await login(page, 'picker');
  await showPicks(page);
  await page.getByRole('combobox', { name: 'Показывать задания' })
    .selectOption('mine');
  const mine = page.locator('.pick-card').filter({ hasText: name });
  await expect(mine).toHaveCount(2);
  const full = mine.filter({ has: page.locator('.pick-qty strong', { hasText: /^2$/ }) });
  await full.getByPlaceholder('Сканируйте или введите').fill('SKU-00001');
  await full.getByRole('spinbutton', { name: 'Фактически отобрано' }).fill('2');
  const picked = page.waitForResponse(r => r.url().includes('/confirm') &&
    r.request().method() === 'POST');
  await full.getByRole('button', { name: 'Подтвердить отбор' }).click();
  expect((await picked).status()).toBe(200);
  const shortage = mine.filter({ has: page.locator('.pick-qty strong', { hasText: /^1$/ }) });
  await shortage.getByPlaceholder('Сканируйте или введите').fill('SKU-00001');
  await shortage.getByRole('spinbutton', { name: 'Фактически отобрано' }).fill('0');
  await expect(shortage.getByRole('status')).toContainText('Недобор: 1');
  page.once('dialog', dialog => dialog.accept());
  const shortResponse = page.waitForResponse(r => r.url().includes('/picks/') &&
    r.url().endsWith('/confirm'));
  await shortage.getByRole('button', { name: 'Зафиксировать недобор' }).click();
  expect((await shortResponse).status()).toBe(200);
  expect(await page.evaluate(() =>
    Object.keys(localStorage).filter(x => x.startsWith('stockpilot_pick_')))).toEqual([]);
  await signOut(page);

  await login(page, 'supervisor');
  await showPicks(page);
  const waiting = page.locator('.pick-card').filter({ hasText: name });
  await expect(waiting).toHaveCount(1);
  const approval = page.waitForResponse(r => r.url().endsWith('/approve-shortage'));
  await waiting.getByRole('button', { name: 'Согласовать недобор' }).click();
  expect((await approval).status()).toBe(200);
});
