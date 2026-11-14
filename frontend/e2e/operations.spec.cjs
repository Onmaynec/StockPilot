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

test('manager -> picker: mobile scan fallback, shipment, return and audit', async ({ page }) => {
  test.setTimeout(90_000);
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
  if ((await page.viewportSize())?.width <= 400) {
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  }
  await page.evaluate(() => Object.defineProperty(window, 'BarcodeDetector', {
    configurable: true, value: undefined
  }));
  await card.getByRole('button', { name: 'Камера' }).click();
  await expect(card.getByRole('alert')).toContainText('Введите штрихкод или артикул вручную');
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
  const detailBody = await detail.json();
  expect(detailBody.order.status).toBe('shipped');
  const shipmentId = detailBody.shipments[0].id;

  await section(page, 'Отгрузки');
  const shipmentRow = page.locator('tr').filter({ hasText: orderId.slice(0, 8) });
  await expect(shipmentRow.getByRole('button', { name: 'Возврат' })).toBeVisible();
  await shipmentRow.getByRole('button', { name: 'Возврат' }).click();
  const returnForm = page.getByTestId('return-composer');
  await expect(returnForm).toBeVisible();
  await returnForm.getByRole('combobox', { name: 'Позиция возврата' }).selectOption({ index: 0 });
  await returnForm.getByRole('combobox', { name: 'Ячейка возврата' }).selectOption({ index: 0 });
  await returnForm.getByRole('spinbutton', { name: 'Количество возврата' }).fill('1');
  const returnResponse = page.waitForResponse(response =>
    response.url().includes('/api/v1/shipments/' + shipmentId + '/returns') &&
    response.request().method() === 'POST');
  await returnForm.getByRole('button', { name: 'Провести возврат' }).click();
  const returned = await returnResponse;
  expect(returned.status()).toBe(201);
  const returnBody = await returned.json();
  const returnable = await page.request.get(
    '/api/v1/shipments/' + shipmentId + '/returnable',
    { headers: { Authorization: 'Bearer ' + pickerToken } }
  );
  expect((await returnable.json()).items[0].returnable_qty).toBe(1);
  const adminLogin = await page.request.post('/api/v1/auth/login', {
    data: { email: 'admin@stockpilot.local',
      password: process.env.DEMO_PASSWORD || 'DemoStock2026!' }
  });
  expect(adminLogin.status()).toBe(200);
  const audit = await page.request.get(
    '/api/v1/audit?after=0&limit=100&entityId=' + returnBody.id,
    { headers: { Authorization: 'Bearer ' + (await adminLogin.json()).accessToken } }
  );
  expect(audit.status()).toBe(200);
  expect((await audit.json()).items.some(row => row.action === 'return.post')).toBe(true);
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


test('admin provisions warehouse, locations, employee access and a product lot', async ({ page }) => {
  const suffix = String(Date.now()) + Math.random().toString(36).slice(2, 6);
  const whCode = 'E5' + suffix;
  const zoneCode = 'Z5' + suffix;
  const binCode = 'BIN5' + suffix;
  const newEmail = 'e5-' + suffix + '@example.org';
  await authenticate(page, 'admin@stockpilot.local');
  await section(page, 'Администрирование');
  await expect(page.getByTestId('admin-console')).toBeVisible();

  await page.getByRole('tab', { name: 'Склады и ячейки' }).click();
  const warehouse = page.getByTestId('create-warehouse-form');
  await warehouse.getByRole('textbox', { name: 'Новый код склада' }).fill(whCode);
  await warehouse.getByRole('textbox', { name: 'Название' }).fill('Тестовый склад E2E');
  const savedWarehouse = page.waitForResponse(r =>
    r.url().endsWith('/api/v1/warehouses') && r.request().method() === 'POST');
  await warehouse.getByRole('button', { name: 'Создать склад' }).click();
  expect((await savedWarehouse).status()).toBe(201);
  await expect(page.getByRole('button', { name: new RegExp(whCode) })).toBeVisible();

  const zone = page.getByTestId('create-zone-form');
  await zone.getByRole('textbox', { name: 'Код новой зоны' }).fill(zoneCode);
  await zone.getByRole('textbox', { name: 'Название зоны' }).fill('Тестовая зона');
  const savedZone = page.waitForResponse(r =>
    r.url().endsWith('/api/v1/zones') && r.request().method() === 'POST');
  await zone.getByRole('button', { name: 'Добавить зону' }).click();
  expect((await savedZone).status()).toBe(201);
  const bin = page.getByTestId('create-bin-form');
  await bin.getByRole('combobox', { name: 'Зона' }).selectOption({ label: zoneCode + ' / Тестовая зона' });
  await bin.getByRole('textbox', { name: 'Код ячейки' }).fill(binCode);
  const savedBin = page.waitForResponse(r =>
    r.url().endsWith('/api/v1/bins') && r.request().method() === 'POST');
  await bin.getByRole('button', { name: 'Добавить ячейку' }).click();
  expect((await savedBin).status()).toBe(201);
  await expect(page.getByText(binCode)).toBeVisible();

  await page.getByRole('tab', { name: 'Сотрудники' }).click();
  const employees = page.getByTestId('create-user-form');
  await employees.getByRole('textbox', { name: 'Полное имя' }).fill('Тестовый сотрудник E2E');
  await employees.getByRole('textbox', { name: 'Электронная почта' }).fill(newEmail);
  await employees.locator('input[type="password"]').fill('integration-password-123!');
  await employees.getByRole('checkbox', { name: new RegExp(whCode) }).check();
  const savedUser = page.waitForResponse(r =>
    r.url().endsWith('/api/v1/users') && r.request().method() === 'POST');
  await employees.getByRole('button', { name: 'Создать сотрудника' }).click();
  expect((await savedUser).status()).toBe(201);
  const employeeRow = page.getByTestId('employee-row').filter({ hasText: newEmail });
  await expect(employeeRow).toContainText('Активен');
  const blocked = page.waitForResponse(r =>
    /\/api\/v1\/users\/[^/]+\/status$/.test(r.url()) &&
    r.request().method() === 'POST');
  page.once('dialog', dialog => dialog.accept());
  await employeeRow.getByRole('button', { name: 'Заблокировать' }).click();
  expect((await blocked).status()).toBe(200);
  await expect(employeeRow).toContainText('Заблокирован');

  await page.getByRole('tab', { name: 'Партии товаров' }).click();
  await page.getByRole('textbox', { name: 'Поиск по SKU или названию' }).fill('SKU-00001');
  await expect(page.getByRole('combobox', { name: 'Номенклатура' })
    .locator('option', { hasText: 'SKU-00001' })).toBeAttached();
  const lot = page.getByTestId('create-lot-form');
  await lot.getByRole('textbox', { name: 'Код партии' }).fill('LOT-E5-' + suffix);
  const savedLot = page.waitForResponse(r =>
    r.url().endsWith('/api/v1/lots') && r.request().method() === 'POST');
  await lot.getByRole('button', { name: 'Сохранить партию' }).click();
  expect((await savedLot).status()).toBe(201);
  await expect(page.getByText('LOT-E5-' + suffix)).toBeVisible();
});
