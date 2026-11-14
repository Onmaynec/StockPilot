import React, { useCallback, useEffect, useState } from 'react';
import { Check, Layers3, PackagePlus, Plus, Search, Users, Warehouse } from 'lucide-react';

const USER_ROLES = {
  manager: 'Менеджер', picker: 'Кладовщик', supervisor: 'Старший смены',
  director: 'Руководитель', admin: 'Администратор'
};
const USER_TABS = [
  { id: 'users', label: 'Сотрудники', icon: Users },
  { id: 'topology', label: 'Склады и ячейки', icon: Warehouse },
  { id: 'lots', label: 'Партии товаров', icon: Layers3 }
];

function Field({ label, children }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}
function Action({ children, disabled, onClick, type = 'button', secondary = false }) {
  return <button className={'btn ' + (secondary ? 'secondary' : 'primary')}
    type={type} disabled={disabled} onClick={onClick}>{children}</button>;
}
function WarehouseChoices({ warehouses, selected, onChange }) {
  return <div className="admin-warehouse-choices" role="group" aria-label="Доступные склады">
    {warehouses.map(item => <label key={item.id} className="admin-check">
      <input type="checkbox" checked={selected.includes(item.id)}
        onChange={e => onChange(e.target.checked
          ? [...selected, item.id]
          : selected.filter(id => id !== item.id))} />
      <span>{item.code} · {item.name}</span>
    </label>)}
    {!warehouses.length && <p className="helper">Сначала создайте склад.</p>}
  </div>;
}

export function AdminConsole({ api, warehouses, onWarehousesChanged, onNotify }) {
  const [tab, setTab] = useState('users');
  const [users, setUsers] = useState([]);
  const [zones, setZones] = useState([]);
  const [bins, setBins] = useState([]);
  const [lots, setLots] = useState([]);
  const [selectedWh, setSelectedWh] = useState('');
  const [editing, setEditing] = useState('');
  const [draft, setDraft] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [newUser, setNewUser] = useState({
    fullName: '', email: '', password: '', role: 'picker', warehouseIds: []
  });
  const [newWarehouse, setNewWarehouse] = useState({ code: '', name: '' });
  const [newZone, setNewZone] = useState({ code: '', name: '' });
  const [newBin, setNewBin] = useState({ code: '', zoneId: '' });
  const [productQuery, setProductQuery] = useState('');
  const [products, setProducts] = useState([]);
  const [productId, setProductId] = useState('');
  const [newLot, setNewLot] = useState({ code: '', expiresAt: '' });
  const warehouseId = selectedWh || warehouses[0]?.id || '';
  const activeWarehouses = warehouses.filter(row => row.active);

  const refresh = useCallback(async () => {
    if (tab === 'users') {
      setUsers(await api('GET', '/users'));
    } else if (tab === 'topology') {
      if (!warehouseId) { setZones([]); setBins([]); return; }
      const [newZones, newBins] = await Promise.all([
        api('GET', '/zones?warehouseId=' + encodeURIComponent(warehouseId)),
        api('GET', '/bins?warehouseId=' + encodeURIComponent(warehouseId))
      ]);
      setZones(newZones); setBins(newBins);
    } else if (tab === 'lots') {
      if (!productId) { setLots([]); return; }
      setLots(await api('GET', '/lots?productId=' + encodeURIComponent(productId)));
    }
  }, [api, tab, warehouseId, productId]);

  useEffect(() => {
    let active = true;
    refresh().catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [refresh]);

  useEffect(() => {
    if (tab !== 'lots') return;
    let active = true;
    const timer = setTimeout(() => {
      api('GET', '/products?q=' + encodeURIComponent(productQuery) + '&limit=30&active=true')
        .then(result => {
          if (!active) return;
          setProducts(result.items || []);
          setProductId(prev => (result.items || []).some(p => p.id === prev)
            ? prev : result.items?.[0]?.id || '');
        }).catch(e => { if (active) setError(e.message); });
    }, 250);
    return () => { active = false; clearTimeout(timer); };
  }, [tab, productQuery, api]);

  async function run(work, success) {
    setBusy(true); setError('');
    try {
      const result = await work();
      onNotify(success);
      await refresh();
      return result;
    } catch (e) {
      setError(e.message);
      return null;
    } finally { setBusy(false); }
  }

  async function createUser(event) {
    event.preventDefault();
    const result = await run(() => api('POST', '/users', {
      ...newUser, fullName: newUser.fullName.trim(), email: newUser.email.trim()
    }), 'Сотрудник создан');
    if (result) setNewUser({ fullName: '', email: '', password: '', role: 'picker', warehouseIds: [] });
  }
  async function changeStatus(employee) {
    if (!window.confirm(employee.active
      ? 'Заблокировать сотрудника и завершить все его сеансы?'
      : 'Восстановить доступ сотрудника?')) return;
    await run(() => api('POST', '/users/' + employee.id + '/status',
      { active: !employee.active }), employee.active ? 'Сотрудник заблокирован' : 'Доступ восстановлен');
  }
  async function applyWarehouseGrants(employee) {
    const result = await run(() => api('POST', '/users/' + employee.id + '/access',
      { warehouseIds: draft }), 'Складские права обновлены. Сеансы сотрудника отозваны.');
    if (result) setEditing('');
  }
  async function createWarehouse(event) {
    event.preventDefault();
    const result = await run(() => api('POST', '/warehouses', newWarehouse), 'Склад создан');
    if (result) {
      setNewWarehouse({ code: '', name: '' });
      await onWarehousesChanged();
      setSelectedWh(result.id);
    }
  }
  async function createZone(event) {
    event.preventDefault();
    const result = await run(() => api('POST', '/zones',
      { warehouseId, code: newZone.code, name: newZone.name }), 'Зона создана');
    if (result) setNewZone({ code: '', name: '' });
  }
  async function createBin(event) {
    event.preventDefault();
    const result = await run(() => api('POST', '/bins', newBin), 'Ячейка создана');
    if (result) setNewBin({ code: '', zoneId: '' });
  }
  async function createLot(event) {
    event.preventDefault();
    const result = await run(() => api('POST', '/lots', {
      productId, code: newLot.code, expiresAt: newLot.expiresAt || null
    }), 'Партия сохранена');
    if (result) setNewLot({ code: '', expiresAt: '' });
  }

  return <div className="admin-console" data-testid="admin-console">
    <div className="admin-tabs" role="tablist" aria-label="Администрирование">
      {USER_TABS.map(item => {
        const Icon = item.icon;
        return <button type="button" key={item.id} role="tab"
          aria-selected={tab === item.id} className={'admin-tab ' + (tab === item.id ? 'selected' : '')}
          onClick={() => { setTab(item.id); setError(''); }}><Icon size={17} />{item.label}</button>;
      })}
    </div>
    {error && <div className="error-note" role="alert">{error}</div>}
    {tab === 'users' && <div className="admin-split">
      <section className="panel admin-panel">
        <h2>Сотрудники</h2>
        <p className="helper">Изменение складских прав и блокировка учётной записи немедленно отзывают прежние сеансы.</p>
        <div className="admin-records">{users.map(employee => <article key={employee.id}
          className="admin-record" data-testid="employee-row">
          <div className="admin-record-main">
            <div><strong>{employee.full_name}</strong><small>{employee.email}</small>
              <small>{USER_ROLES[employee.role] || employee.role}
                {' · '}{(employee.warehouse_ids || []).length} скл.</small></div>
            <span className={'admin-user-status ' + (employee.active ? 'enabled' : 'disabled')}>
              {employee.active ? 'Активен' : 'Заблокирован'}</span>
          </div>
          <div className="admin-record-actions">
            <Action secondary disabled={busy} onClick={() => {
              setEditing(editing === employee.id ? '' : employee.id);
              setDraft(employee.warehouse_ids || []);
            }}>Складские права</Action>
            <Action secondary disabled={busy} onClick={() => changeStatus(employee)}>
              {employee.active ? 'Заблокировать' : 'Разблокировать'}</Action>
          </div>
          {editing === employee.id && <div className="admin-grants">
            <strong>Склады сотрудника</strong>
            <WarehouseChoices warehouses={activeWarehouses} selected={draft} onChange={setDraft} />
            <Action disabled={busy} onClick={() => applyWarehouseGrants(employee)}>
              <Check size={16}/> Сохранить доступ</Action>
          </div>}
        </article>)}</div>
        {!users.length && <p className="helper">Сотрудники ещё не загружены.</p>}
      </section>
      <section className="panel admin-panel">
        <h2><Users size={19} /> Новый сотрудник</h2>
        <form onSubmit={createUser} data-testid="create-user-form">
          <Field label="Полное имя"><input required maxLength={250} value={newUser.fullName}
            onChange={e => setNewUser(v => ({ ...v, fullName: e.target.value }))}/></Field>
          <Field label="Электронная почта"><input required type="email" value={newUser.email}
            onChange={e => setNewUser(v => ({ ...v, email: e.target.value }))}/></Field>
          <Field label="Начальный пароль (минимум 12 символов)"><input required type="password"
            minLength={12} autoComplete="new-password" value={newUser.password}
            onChange={e => setNewUser(v => ({ ...v, password: e.target.value }))}/></Field>
          <Field label="Роль"><select value={newUser.role}
            onChange={e => setNewUser(v => ({ ...v, role: e.target.value }))}>
            {Object.entries(USER_ROLES).map(([value, label]) =>
              <option key={value} value={value}>{label}</option>)}
          </select></Field>
          <strong className="admin-subtitle">Доступ к складам</strong>
          <WarehouseChoices warehouses={activeWarehouses} selected={newUser.warehouseIds}
            onChange={warehouseIds => setNewUser(v => ({ ...v, warehouseIds }))}/>
          <Action type="submit" disabled={busy}><Plus size={16}/> Создать сотрудника</Action>
        </form>
      </section>
    </div>}

    {tab === 'topology' && <div className="admin-stack">
      <section className="panel admin-panel">
        <h2><Warehouse size={19}/> Структура складов</h2>
        <div className="admin-warehouse-grid">
          {warehouses.map(wh => <button type="button" key={wh.id}
            className={'admin-warehouse-option ' + (warehouseId === wh.id ? 'selected' : '')}
            onClick={() => { setSelectedWh(wh.id); setNewBin({ code: '', zoneId: '' }); }}>
            <Warehouse size={19}/><strong>{wh.code}</strong><span>{wh.name}</span></button>)}
        </div>
        <form className="admin-inline-form" onSubmit={createWarehouse}
          data-testid="create-warehouse-form">
          <Field label="Новый код склада"><input required maxLength={32}
            value={newWarehouse.code}
            onChange={e => setNewWarehouse(v => ({ ...v, code: e.target.value }))}/></Field>
          <Field label="Название"><input required maxLength={150} value={newWarehouse.name}
            onChange={e => setNewWarehouse(v => ({ ...v, name: e.target.value }))}/></Field>
          <Action type="submit" disabled={busy}><Plus size={16}/> Создать склад</Action>
        </form>
      </section>
      {warehouseId && <div className="admin-split">
        <section className="panel admin-panel">
          <h2><Layers3 size={19}/> Зоны склада</h2>
          {zones.map(z => <div className="detail-row" key={z.id}>
            <span><strong>{z.code}</strong> · {z.name}</span>
            <span>{z.active ? 'Активна' : 'Архив'}</span></div>)}
          <form onSubmit={createZone} data-testid="create-zone-form">
            <Field label="Код новой зоны"><input required maxLength={50}
              value={newZone.code}
              onChange={e => setNewZone(v => ({ ...v, code: e.target.value }))}/></Field>
            <Field label="Название зоны"><input required maxLength={150}
              value={newZone.name}
              onChange={e => setNewZone(v => ({ ...v, name: e.target.value }))}/></Field>
            <Action type="submit" disabled={busy}><Plus size={16}/> Добавить зону</Action>
          </form>
        </section>
        <section className="panel admin-panel">
          <h2><PackagePlus size={19}/> Ячейки</h2>
          {bins.map(bin => <div className="detail-row" key={bin.id}>
            <span>{bin.zone_code} / <strong>{bin.code}</strong></span>
            <span>{bin.active ? 'Активна' : 'Архив'}</span></div>)}
          <form onSubmit={createBin} data-testid="create-bin-form">
            <Field label="Зона"><select value={newBin.zoneId}
              onChange={e => setNewBin(v => ({ ...v, zoneId: e.target.value }))} required>
              <option value="">Выберите зону</option>
              {zones.filter(z => z.active).map(z => <option key={z.id} value={z.id}>
                {z.code} / {z.name}</option>)}
            </select></Field>
            <Field label="Код ячейки"><input required maxLength={50} value={newBin.code}
              onChange={e => setNewBin(v => ({ ...v, code: e.target.value }))}/></Field>
            <Action type="submit" disabled={busy || !zones.some(z => z.active)}>
              <Plus size={16}/> Добавить ячейку</Action>
          </form>
        </section>
      </div>}
    </div>}

    {tab === 'lots' && <div className="admin-split">
      <section className="panel admin-panel">
        <h2><Search size={19}/> Выбор товара</h2>
        <Field label="Поиск по SKU или названию"><input value={productQuery}
          onChange={e => setProductQuery(e.target.value)}
          placeholder="Поиск номенклатуры"/></Field>
        <Field label="Номенклатура"><select value={productId}
          onChange={e => setProductId(e.target.value)}>
          {!products.length && <option value="">Ничего не найдено</option>}
          {products.map(p => <option key={p.id} value={p.id}>{p.sku} · {p.name}</option>)}
        </select></Field>
        <h3>Зарегистрированные партии</h3>
        {lots.map(lot => <div className="detail-row" key={lot.id}>
          <strong>{lot.code}</strong><span>Годен до: {lot.expires_at || 'не задано'}</span>
        </div>)}
        {productId && !lots.length && <p className="helper">Партий пока нет.</p>}
      </section>
      <section className="panel admin-panel">
        <h2><Plus size={19}/> Зарегистрировать партию</h2>
        <form onSubmit={createLot} data-testid="create-lot-form">
          <Field label="Код партии"><input required maxLength={50} value={newLot.code}
            onChange={e => setNewLot(v => ({ ...v, code: e.target.value }))}/></Field>
          <Field label="Срок годности (необязательно)"><input type="date" value={newLot.expiresAt}
            onChange={e => setNewLot(v => ({ ...v, expiresAt: e.target.value }))}/></Field>
          <p className="helper">Партия относится к выбранному товару. Существующие партии не удаляются.</p>
          <Action type="submit" disabled={busy || !productId}><Plus size={16}/> Сохранить партию</Action>
        </form>
      </section>
    </div>}
  </div>;
}
