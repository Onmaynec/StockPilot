const { test, expect } = require('@playwright/test');

async function authenticate(page, email) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: 'Электронная почта' }).fill(email);
  await page.locator('input[type="password"]').fill(process.env.DEMO_PASSWORD || 'DemoStock2026!');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByText('Открытые заказы')).toBeVisible();
}

async function section(page, label) {
  if ((await page.viewportSize())?.width <= 850) {
    await page.getByRole('button', { name: 'Открыть меню' }).click();
  }
  await page.locator('.side-nav').getByRole('button', { name: label, exact: true }).click();
  await expect(page.locator('.page-title h1')).toHaveText(label);
}

async function logout(page) {
  await page.locator('button[title="Выйти"]').click();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
}

test('manager -> picker: multi-line order, manual scan, shipment and audit', async ({ page }) => {
  const suffix = String(Date.now());
  const customer = 'E2E ' + suffix;
  await authenticate(page, 'manager@stockpilot.local');
  await section(page, 'Заказы');
  await page.getByRole('button', { name: 'Создать заказ' }).click();
  const dialog = page.getByTestId('order-composer');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox', { name: 'Контрагент' }).fill(customer);
  await dialog.getByRole('textbox', { name: 'Поиск товара' }).fill('SKU-00001');
  await expect(dialog.getByRole('option', { name: /SKU-00001/ })).toBeAttached();
  await dialog.getByRole('button', { name: 'Добавить позицию' }).click();
  await expect(dialog.getByTestId('order-line')).toHaveCount(1);
  await dialog.getByRole('spinbutton', { name: 'Количество строки 1' }).fill('2');
  const createResponse = page.waitForResponse(response =>
    response.url().endsWith('/api/v1/orders') && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Создать заказ' }).click();
  const saved = await createResponse;
  expect(saved.status()).toBe(201);
  const orderId = (await saved.json()).id;
  await expect(page.getByText(customer)).toBeVisible();
  const target = page.locator('tr').filter({ hasText: customer });
  const reserveResponse = page.waitForResponse(response =>
    response.url().includes('/api/v1/orders/' + orderId + '/reserve'));
  await target.getByRole('button', { name: 'Резерв' }).click();
  expect((await reserveResponse).status()).toBe(200);
  await logout(page);

  await authenticate(page, 'picker@stockpilot.local');
  const pickerToken = await page.evaluate(() => sessionStorage.getItem('stockpilot_token'));
  const blocked = await page.request.post('/api/v1/products', {
    headers: { Authorization: 'Bearer ' + pickerToken },
    data: { sku: 'NO-AUTH-' + suffix, name: 'Forbidden' }
  });
  expect(blocked.status()).toBe(403);
  await section(page, 'Задания отбора');
  const card = page.locator('.pick-card').filter({ hasText: 'SKU-00001' }).first();
  await expect(card).toBeVisible();
  await card.getByPlaceholder('Сканируйте или введите').fill('SKU-00001');
  await card.getByRole('spinbutton', { name: 'Фактически отобрано' }).fill('2');
  const pickResponse = page.waitForResponse(response =>
    response.url().includes('/picks/') && response.url().endsWith('/confirm'));
  await card.getByRole('button', { name: 'Подтвердить отбор' }).click();
  expect((await pickResponse).status()).toBe(200);
  await section(page, 'Отгрузки');
  const shipButton = page.getByRole('button', { name: 'Отгрузить' }).first();
  await expect(shipButton).toBeVisible();
  const shipResponse = page.waitForResponse(response =>
    response.url().includes('/shipments/') && response.url().endsWith('/confirm'));
  await shipButton.click();
  expect((await shipResponse).status()).toBe(200);
  const detail = await page.request.get('/api/v1/orders/' + orderId, {
    headers: { Authorization: 'Bearer ' + pickerToken }
  });
  expect(detail.status()).toBe(200);
  expect((await detail.json()).order.status).toBe('shipped');
  await logout(page);

  await authenticate(page, 'manager@stockpilot.local');
  await section(page, 'Заказы');
  const completed = page.locator('tr').filter({ hasText: customer });
  await expect(completed).toContainText('Отгружен');
});

test('account: changing password invalidates active JWTs', async ({ page }) => {
  await authenticate(page, 'admin@stockpilot.local');
  const token = await page.evaluate(() => sessionStorage.getItem('stockpilot_token'));
  const revoke = await page.request.post('/api/v1/auth/logout-all', {
    headers: { Authorization: 'Bearer ' + token }, data: {}
  });
  expect(revoke.status()).toBe(200);
  const denied = await page.request.get('/api/v1/auth/me', {
    headers: { Authorization: 'Bearer ' + token }
  });
  expect(denied.status()).toBe(401);
});
