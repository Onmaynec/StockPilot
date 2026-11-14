import React, { useEffect, useMemo, useRef, useState } from 'react';
import { OrderComposer, ReturnComposer, SecurityComposer } from './workflows';
import { AdminConsole } from './AdminConsole';
import {
  Activity, ArrowDownLeft, ArrowLeftRight, ArrowUpRight, Barcode, Boxes, Check,
  ChevronDown, ChevronRight, ClipboardCheck, ClipboardList, FileSpreadsheet,
  LayoutDashboard, LogOut, Menu, Package, Plus, RefreshCw, ScanLine,
  Search, ShieldCheck, Truck, Warehouse, X
} from 'lucide-react';

const NAV = [
  { id: 'dashboard', label: 'Обзор', icon: LayoutDashboard },
  { id: 'stocks', label: 'Остатки', icon: Boxes },
  { id: 'anomalies', label: 'Контроль остатков', icon: ShieldCheck },
  { id: 'products', label: 'Номенклатура', icon: Package },
  { id: 'receipts', label: 'Поступления', icon: ArrowDownLeft },
  { id: 'orders', label: 'Заказы', icon: ClipboardList },
  { id: 'picks', label: 'Задания отбора', icon: ScanLine },
  { id: 'shipments', label: 'Отгрузки', icon: Truck },
  { id: 'transfers', label: 'Перемещения', icon: ArrowLeftRight },
  { id: 'inventory', label: 'Инвентаризации', icon: ClipboardCheck },
  { id: 'audit', label: 'Журнал аудита', icon: ShieldCheck },
  { id: 'import', label: 'Импорт CSV', icon: FileSpreadsheet },
  { id: 'admin', label: 'Администрирование', icon: ShieldCheck },
];
const LABELS = {
  draft: 'Черновик', posted: 'Проведён', confirmed: 'Подтверждён',
  picking: 'В сборке', picked: 'Собран', shipped: 'Отгружен',
  cancelled: 'Отменён', pending: 'Ожидает', shortage_pending: 'Недобор',
  completed: 'Завершён', accepted: 'Согласован', counting: 'Пересчёт',
  submitted: 'На согласовании', approved: 'Утверждён',
};
const ROLE_LABELS = {
  admin: 'Администратор', manager: 'Менеджер',
  picker: 'Кладовщик', supervisor: 'Старший смены', director: 'Руководитель'
};
const idKey = () => (typeof crypto !== 'undefined' && crypto.randomUUID
  ? crypto.randomUUID() : 'req_' + Date.now() + '_' + Math.random());
const short = value => String(value || '—').slice(0, 8);
const date = value => value ? new Date(value).toLocaleString('ru-RU') : '—';
const quantity = value => Number(value || 0).toLocaleString('ru-RU');

async function api(method, path, body, idempotent = false) {
  const token = sessionStorage.getItem('stockpilot_token');
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  if (idempotent) headers['Idempotency-Key'] = idKey();
  let response;
  try {
    response = await fetch('/api/v1' + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch {
    throw new Error('Нет соединения с сервером. Данные не отправлены.');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.details;
    const suffix = detail && detail.available !== undefined
      ? ' (доступно: ' + detail.available + ')' : '';
    throw new Error((payload.message || 'Ошибка операции') + suffix);
  }
  return payload;
}

function Pill({ status }) {
  const good = ['posted', 'shipped', 'completed', 'accepted', 'approved'];
  const bad = ['cancelled', 'shortage_pending', 'expired'];
  const tone = good.includes(status) ? 'success' : bad.includes(status) ? 'danger' : 'neutral';
  return <span className={'status ' + tone}><span className="status-dot" />{LABELS[status] || status}</span>;
}
function Empty({ message = 'Записей пока нет', action }) {
  return <div className="empty"><Boxes size={32} strokeWidth={1.4} /><strong>{message}</strong>
    <span>Информация появится после проведения первой операции.</span>{action}</div>;
}
function Btn({ children, icon: Icon, onClick, variant = 'secondary', disabled, type = 'button' }) {
  return <button type={type} className={'btn ' + variant} onClick={onClick} disabled={disabled}>
    {Icon && <Icon size={16} />}{children}</button>;
}
function Stat({ label, value, hint, icon: Icon, emphasis }) {
  return <div className={'stat ' + (emphasis ? 'stat-emphasis' : '')}>
    <div className="stat-head"><span>{label}</span><Icon size={20} /></div>
    <div className="stat-value">{value}</div><div className="stat-hint">{hint}</div>
  </div>;
}
function SectionHeader({ title, subtitle, children }) {
  return <div className="section-header"><div><h2>{title}</h2><p>{subtitle}</p></div>
    <div className="header-actions">{children}</div></div>;
}

function PickCard({
  task, onConfirm, onApprove, onAssign, canConfirm, canApprove, canAssign,
  assignees, currentUserId, busy
}) {
  // Drafts are short-lived and isolated by employee + task within this browser tab.
  // They never contain credentials and do not survive a browser session.
  const key = 'stockpilot_pick_' + currentUserId + '_' + task.id;
  const initial = () => {
    try { return JSON.parse(sessionStorage.getItem(key) || '{}'); } catch { return {}; }
  };
  const [barcode, setBarcode] = useState(() => initial().barcode || '');
  const [actual, setActual] = useState(() => initial().actual ?? task.expected_qty);
  const [camera, setCamera] = useState(false);
  const [scanError, setScanError] = useState('');
  const video = useRef(null);
  const stream = useRef(null);
  const timer = useRef(null);
  const detecting = useRef(false);
  const active = task.status === 'pending';
  const expected = Number(task.expected_qty);
  const picked = Number(actual);
  const validPick = actual !== '' && Number.isInteger(picked) && picked >= 0 && picked <= expected;
  const shortPick = validPick && picked < expected;
  const eligible = assignees.filter(person => (person.warehouse_ids || []).includes(task.warehouse_id));
  const assignedName = task.assigned_to
    ? task.assigned_to === currentUserId ? 'Вам'
      : eligible.find(person => person.id === task.assigned_to)?.full_name || 'Другому сотруднику'
    : 'Не назначено';

  useEffect(() => {
    if (active) {
      sessionStorage.setItem(key, JSON.stringify({ barcode, actual }));
    } else {
      sessionStorage.removeItem(key);
    }
  }, [key, active, barcode, actual]);
  const stop = () => {
    clearInterval(timer.current);
    if (stream.current) stream.current.getTracks().forEach(track => track.stop());
    stream.current = null;
    detecting.current = false;
    setCamera(false);
  };
  useEffect(() => {
    const stopWhenHidden = () => {
      if (!document.hidden || !stream.current) return;
      clearInterval(timer.current);
      stream.current.getTracks().forEach(track => track.stop());
      stream.current = null;
      detecting.current = false;
      setCamera(false);
    };
    document.addEventListener('visibilitychange', stopWhenHidden);
    return () => {
      document.removeEventListener('visibilitychange', stopWhenHidden);
      clearInterval(timer.current);
      if (stream.current) stream.current.getTracks().forEach(track => track.stop());
      stream.current = null;
      detecting.current = false;
    };
  }, []);
  async function scan() {
    if (camera) { stop(); return; }
    setScanError('');
    try {
      if (typeof window.BarcodeDetector !== 'function') {
        throw new Error('Автоматическое распознавание недоступно в этом браузере');
      }
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна');
      const media = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } }
      });
      stream.current = media;
      setCamera(true);
      const detector = new window.BarcodeDetector();
      setTimeout(() => {
        if (video.current && stream.current) {
          video.current.srcObject = media;
          video.current.play().catch(() => {});
        }
      }, 0);
      timer.current = setInterval(async () => {
        if (!video.current || !stream.current || detecting.current) return;
        detecting.current = true;
        try {
          const codes = await detector.detect(video.current);
          if (codes[0]?.rawValue && stream.current) {
            setBarcode(codes[0].rawValue);
            stop();
          }
        } catch { /* камера может ещё запускаться или не распознать кадр */ }
        finally { detecting.current = false; }
      }, 500);
    } catch (err) {
      setScanError((err.message || 'Не удалось открыть камеру') +
        '. Введите штрихкод или артикул вручную.');
      stop();
    }
  }

  return <article className={'pick-card ' + (shortPick && active ? 'pick-card-short' : '')}
    data-testid="pick-task">
    <div className="pick-top">
      <span className="eyebrow">{task.bin_code} / {short(task.warehouse_id)}</span>
      <Pill status={task.status} />
    </div>
    <div className="pick-title">{task.name}</div>
    <div className="pick-sku">{task.sku} <span>· {task.customer}</span></div>
    <div className="pick-context">Заказ OR-{short(task.order_id)}
      <span> · Исполнитель: {assignedName}</span></div>
    <div className="pick-qty"><strong>{task.expected_qty}</strong><span>ед. к отбору</span></div>
    {active && canAssign && <label className="field">
      <span>Назначить кладовщика</span>
      <select aria-label={'Исполнитель ' + task.sku} value={task.assigned_to || ''}
        disabled={busy || !eligible.length}
        onChange={e => { if (e.target.value) onAssign(task.id, e.target.value); }}>
        <option value="" disabled>{eligible.length ? 'Выберите сотрудника' : 'Нет доступных кладовщиков'}</option>
        {eligible.map(person => <option key={person.id} value={person.id}>
          {person.full_name}</option>)}
      </select>
    </label>}
    {active && canConfirm ? <>
      <label className="field"><span>Штрихкод / SKU</span><div className="input-with-icon">
        <Barcode size={17} /><input placeholder="Сканируйте или введите"
          aria-label={'Штрихкод / SKU ' + task.sku}
          value={barcode} onChange={e => setBarcode(e.target.value)}
          autoComplete="off" autoCapitalize="off" spellCheck={false} /></div></label>
      <div className="inline-fields"><label className="field">
        <span>Фактически отобрано</span>
        <input type="number" min="0" max={expected} step="1"
          value={actual} onChange={e => setActual(e.target.value)}
          aria-label="Фактически отобрано" inputMode="numeric" /></label>
        <Btn icon={ScanLine} onClick={scan}>{camera ? 'Закрыть' : 'Камера'}</Btn></div>
      {camera && <video ref={video} className="scanner-preview" autoPlay playsInline muted />}
      {scanError && <p className="pick-scan-error" role="alert">{scanError}</p>}
      {shortPick && <div className="pick-shortage-warning" role="status">
        Недобор: {expected - picked} шт. После подтверждения потребуется решение старшего смены.
      </div>}
      {!validPick && <p className="pick-scan-error" role="alert">
        Укажите целое количество от 0 до {expected}.
      </p>}
      <Btn variant="primary" icon={Check} disabled={busy || !barcode.trim() || !validPick}
        onClick={async () => {
          if (shortPick && !window.confirm(
            'Подтвердить недобор ' + (expected - picked) + ' шт.? ' +
            'Задание будет ожидать старшего смены.'
          )) return;
          const ok = await onConfirm(task.id, barcode.trim(), picked);
          if (ok) { sessionStorage.removeItem(key); stop(); }
        }}>{shortPick ? 'Зафиксировать недобор' : 'Подтвердить отбор'}</Btn>
    </> : <p className="helper">
      {active && !canConfirm ? 'Задание назначено другому сотруднику или недоступно вашей роли. ' : ''}
      Факт: {task.actual_qty ?? '—'} из {task.expected_qty}.
      {task.status === 'shortage_pending' ? ' Требуется решение старшего смены.' : ''}
    </p>}
    {task.status === 'shortage_pending' && canApprove &&
      <Btn variant="primary" icon={Check} disabled={busy}
        onClick={() => onApprove(task.id)}>Согласовать недобор</Btn>}
  </article>;
}

function Login({ onLogin, busy, message }) {
  const [email, setEmail] = useState('admin@stockpilot.local');
  const [password, setPassword] = useState('');
  return <div className="login-shell">
    <div className="login-art"><div className="logo huge"><span className="logo-mark"><Boxes size={28} /></span>
      <span>stockpilot<span className="logo-period">.</span></span></div>
      <div className="login-art-bottom"><span className="eyebrow">WAREHOUSE OPERATIONS / 01</span>
        <h1>Контроль каждого<br />движения.</h1>
        <p>Один источник данных для склада, менеджеров и руководства.</p>
        <div className="fine-line" /></div></div>
    <div className="login-panel"><div className="login-form">
      <span className="eyebrow accent">ДОБРО ПОЖАЛОВАТЬ</span>
      <h2>Войти в систему</h2><p>Введите учётные данные, предоставленные администратором.</p>
      <form onSubmit={e => { e.preventDefault(); onLogin(email, password); }}>
        <label className="field"><span>Электронная почта</span>
          <input type="email" autoComplete="username" value={email}
            onChange={e => setEmail(e.target.value)} required /></label>
        <label className="field"><span>Пароль</span><input type="password"
          autoComplete="current-password" value={password}
          onChange={e => setPassword(e.target.value)} required /></label>
        {message && <div className="error-note">{message}</div>}
        <Btn type="submit" variant="primary" disabled={busy}>Войти <ChevronRight size={16} /></Btn>
      </form><div className="login-footer">StockPilot WMS / защищённый вход</div>
    </div></div>
  </div>;
}

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('stockpilot_token'));
  const [user, setUser] = useState(null);
  const [screen, setScreen] = useState('dashboard');
  const [navOpen, setNavOpen] = useState(false);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [dashboard, setDashboard] = useState({});
  const [warehouses, setWarehouses] = useState([]);
  const [warehouseId, setWarehouseId] = useState('');
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [modal, setModal] = useState(null);
  const [form, setForm] = useState({});
  const [productOptions, setProductOptions] = useState([]);
  const [binOptions, setBinOptions] = useState([]);
  const [lotOptions, setLotOptions] = useState([]);
  const [zoneOptions, setZoneOptions] = useState([]);
  const [detail, setDetail] = useState(null);
  const [pickFilter, setPickFilter] = useState('active');
  const [pickAssignees, setPickAssignees] = useState([]);
  const [adminRefresh, setAdminRefresh] = useState(0);

  const current = NAV.find(item => item.id === screen) || NAV[0];
  const isAdmin = user?.role === 'admin';
  const canWrite = !['director'].includes(user?.role);
  const canManage = ['admin', 'manager'].includes(user?.role);
  const canCount = ['admin', 'supervisor'].includes(user?.role);
  const canOperate = ['admin', 'picker', 'supervisor'].includes(user?.role);

  function alertMessage(message) {
    setNotice(message);
    setTimeout(() => setNotice(''), 6000);
  }
  async function login(email, password) {
    setBusy(true);
    try {
      const response = await api('POST', '/auth/login', { email, password });
      sessionStorage.setItem('stockpilot_token', response.accessToken);
      setToken(response.accessToken);
      setNotice('');
    } catch (e) { alertMessage(e.message); }
    finally { setBusy(false); }
  }
  function logout() {
    sessionStorage.removeItem('stockpilot_token');
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const key = sessionStorage.key(i);
      if (key?.startsWith('stockpilot_pick_')) sessionStorage.removeItem(key);
    }
    setToken(null); setUser(null); setModal(null);
    setScreen('dashboard'); setPickFilter('active');
    setRows([]); setPickAssignees([]); setWarehouseId('');
  }
  useEffect(() => {
    // Clean up unsafely persistent drafts created by older releases.
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (key?.startsWith('stockpilot_pick_')) localStorage.removeItem(key);
      }
    } catch { /* storage may be restricted */ }
  }, []);
  useEffect(() => {
    if (!token) return;
    Promise.all([api('GET', '/auth/me'), api('GET', '/warehouses')])
      .then(([me, wh]) => { setUser(me); setWarehouses(wh); })
      .catch(e => { alertMessage(e.message); logout(); });
  }, [token]);

  useEffect(() => {
    if (screen !== 'picks' || !user || !['admin', 'supervisor'].includes(user.role)) {
      setPickAssignees([]);
      return;
    }
    let cancelled = false;
    const path = '/picks/assignees' + (warehouseId
      ? '?warehouseId=' + encodeURIComponent(warehouseId) : '');
    api('GET', path).then(result => {
      if (!cancelled) setPickAssignees(result);
    }).catch(err => { if (!cancelled) alertMessage(err.message); });
    return () => { cancelled = true; };
  }, [screen, user?.id, user?.role, warehouseId]);

  const query = new URLSearchParams();
  if (warehouseId) query.set('warehouseId', warehouseId);
  if (appliedSearch) query.set(screen === 'stocks' ? 'sku' : 'q', appliedSearch);
  query.set('page', page);
  const queryStr = query.toString();

  async function reload() {
    if (!user) return;
    setLoading(true);
    try {
      if (screen === 'dashboard') {
        const data = await api('GET', '/dashboard');
        setDashboard(data); setRows([]);
      } else if (screen === 'import') { setRows([]); }
      else {
        const paths = {
          stocks: '/stocks?' + queryStr,
          anomalies: '/stocks/anomalies' + (warehouseId
            ? '?warehouseId=' + encodeURIComponent(warehouseId) : ''),
          products: '/products?q=' + encodeURIComponent(appliedSearch) + '&page=' + page,
          receipts: '/receipts', orders: '/orders', picks: '/picks' + (warehouseId || pickFilter === 'mine'
            ? '?' + new URLSearchParams({
                ...(warehouseId ? { warehouseId } : {}),
                ...(pickFilter === 'mine' ? { assignedToMe: 'true' } : {})
              }).toString() : ''),
          shipments: '/shipments', transfers: '/transfers',
          inventory: '/inventory', audit: '/audit?after=0&limit=100'
        };
        if (paths[screen]) {
          const data = await api('GET', paths[screen]);
          setRows(Array.isArray(data) ? data : data.items || []);
          setTotal(data.total || 0);
        }
      }
    } catch (e) { alertMessage(e.message); }
    finally { setLoading(false); }
  }
  useEffect(() => { reload(); }, [screen, user?.id, warehouseId, appliedSearch, page, pickFilter]);

  async function action(method, path, body, idem = false, success = 'Операция выполнена') {
    setBusy(true);
    try {
      const result = await api(method, path, body, idem);
      alertMessage(success);
      await reload();
      return result;
    } catch (e) { alertMessage(e.message); return null; }
    finally { setBusy(false); }
  }
  async function openForm(type, item = null) {
    setForm({}); setDetail(null); setModal({ type, item });
    try {
      if (['receipt', 'transfer', 'block'].includes(type)) {
        const products = await api('GET', '/products?limit=100');
        setProductOptions(products.items || []);
      }
      if (['receipt', 'transfer', 'block'].includes(type) && warehouses[0]) {
        const wh = warehouseId || warehouses[0].id;
        const bins = await api('GET', '/bins?warehouseId=' + wh);
        setBinOptions(bins || []);
      }
      if (type === 'count' && warehouses[0]) {
        const wh = warehouseId || warehouses[0].id;
        setZoneOptions(await api('GET', '/zones?warehouseId=' + wh));
      }
      if (type === 'receipt-confirm') {
        const receipt = await api('GET', '/receipts/' + item.id);
        setDetail(receipt);
        setForm({ items: receipt.items.map(line => ({
          lineId: line.id, actualQty: line.planned_qty, reason: ''
        })) });
      }
      if (type === 'inventory-variance') {
        setDetail(await api('GET', '/inventory/' + item.id + '/variance'));
      }
      if (type === 'inventory-count') {
        const count = await api('GET', '/inventory/' + item.id);
        setDetail(count);
        setForm({ items: count.items.map(line => ({
          productId: line.product_id, binId: line.bin_id, lotId: line.lot_id,
          actualQty: line.actual_qty ?? line.snapshot_qty
        })) });
      }
    } catch (e) { alertMessage(e.message); }
  }
  useEffect(() => {
    if (!form.productId) { setLotOptions([]); return; }
    api('GET', '/lots?productId=' + form.productId).then(setLotOptions).catch(() => {});
  }, [form.productId]);
  useEffect(() => {
    const wh = form.warehouseId;
    if (!wh) return;
    api('GET', '/bins?warehouseId=' + wh).then(setBinOptions).catch(() => {});
    api('GET', '/zones?warehouseId=' + wh).then(setZoneOptions).catch(() => {});
  }, [form.warehouseId]);

  const set = (key, value) => setForm(prev => ({ ...prev, [key]: value }));
  const selectField = (label, key, options, placeholder = 'Выберите значение') =>
    <label className="field"><span>{label}</span><select required value={form[key] || ''}
      onChange={e => set(key, e.target.value)}>
      <option value="">{placeholder}</option>
      {options.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
    </select></label>;
  const whField = () => selectField('Склад', 'warehouseId',
    warehouses.map(w => ({ value: w.id, label: w.code + ' — ' + w.name })));
  const productField = () => selectField('Товар', 'productId',
    productOptions.map(p => ({ value: p.id, label: p.sku + ' · ' + p.name })));
  const binField = (key = 'binId', label = 'Ячейка') => selectField(label, key,
    binOptions.map(b => ({ value: b.id, label: b.zone_code + ' / ' + b.code })));
  const lotField = () => selectField('Партия', 'lotId',
    lotOptions.map(l => ({ value: l.id, label: l.code + (l.expires_at ? ' · ' + l.expires_at : '') })));
  const inputField = (label, key, type = 'text', required = true, placeholder = '') =>
    <label className="field"><span>{label}</span><input type={type} required={required}
      min={type === 'number' ? '0' : undefined} value={form[key] ?? ''}
      placeholder={placeholder} onChange={e => set(key, e.target.value)} /></label>;
  const num = key => Number(form[key]);

  async function submitModal(e) {
    e.preventDefault();
    const type = modal?.type, item = modal?.item;
    let result = null;
    if (type === 'product') result = await action('POST', '/products', {
      sku: form.sku, name: form.name, barcode: form.barcode || null,
      category: form.category || 'Общее', pickPolicy: form.pickPolicy || 'FIFO'
    });
    if (type === 'receipt') result = await action('POST', '/receipts', { items: [{
      productId: form.productId, binId: form.binId, lotId: form.lotId, plannedQty: num('qty')
    }] });
    if (type === 'transfer') result = await action('POST', '/transfers', {
      productId: form.productId, lotId: form.lotId, fromBinId: form.binId,
      toBinId: form.toBinId, qty: num('qty')
    });
    if (type === 'count') result = await action('POST', '/inventory',
      { zoneId: form.zoneId });
    if (type === 'receipt-confirm') result = await action('POST',
      '/receipts/' + item.id + '/confirm', { items: form.items }, true);
    if (type === 'inventory-count') result = await action('POST',
      '/inventory/' + item.id + '/count', { items: form.items });
    if (result) setModal(null);
  }

  async function downloadCsv() {
    const suffix = screen === 'stocks'
      ? '/export/stocks.csv' + (warehouseId ? '?warehouseId=' + encodeURIComponent(warehouseId) : '')
      : '/export/products.csv';
    try {
      const response = await fetch('/api/v1' + suffix, {
        headers: { Authorization: 'Bearer ' + sessionStorage.getItem('stockpilot_token') }
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.message || 'Не удалось экспортировать CSV');
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = screen === 'stocks' ? 'stockpilot-stocks.csv' : 'stockpilot-products.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (e) { alertMessage(e.message); }
  }

  const go = to => {
    setScreen(to); setPage(1); setAppliedSearch(''); setSearch(''); setNavOpen(false);
  };

  if (!token || !user) return <Login onLogin={login} busy={busy} message={notice} />;
  return <div className="app-shell">
    {navOpen && <div className="mobile-scrim" onClick={() => setNavOpen(false)} />}
    <aside className={'sidebar ' + (navOpen ? 'open' : '')}>
      <div className="brand"><span className="logo-mark"><Boxes size={24} /></span>
        <span>stockpilot<span className="logo-period">.</span></span>
        <button className="mobile-close" onClick={() => setNavOpen(false)}><X size={19} /></button></div>
      <div className="workspace"><div className="workspace-icon"><Warehouse size={18} /></div>
        <div><strong>NordTrade</strong><small>Складская платформа</small></div><ChevronDown size={15} /></div>
      <div className="nav-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav className="side-nav">{NAV.filter(n => {
        if (user.role === 'director') return ['dashboard', 'audit', 'inventory', 'stocks', 'products', 'anomalies'].includes(n.id);
        if (['audit', 'anomalies'].includes(n.id)) return ['admin', 'supervisor', 'director'].includes(user.role);
        if (['import','admin'].includes(n.id)) return user.role === 'admin';
        return true;
      }).map(item => {
        const Icon = item.icon;
        return <button key={item.id} className={'nav-entry ' + (screen === item.id ? 'selected' : '')}
          onClick={() => go(item.id)}><Icon size={19} strokeWidth={1.8} />
          <span>{item.label}</span>{screen === item.id && <span className="nav-indicator" />}</button>;
      })}</nav>
      <div className="sidebar-bottom"><div className="system-status"><span className="live-indicator" />
        Серверная проверка операций</div><div className="version">STOCKPILOT WMS <span>v1.0.0</span></div></div>
    </aside>

    <main className="main">
      <header className="topbar">
        <button className="hamburger" onClick={() => setNavOpen(true)} aria-label="Открыть меню">
          <Menu size={22} /></button>
        <div className="breadcrumbs"><span>StockPilot</span><ChevronRight size={16} /><strong>{current.label}</strong></div>
        <div className="topbar-right"><span className="environment-tag"><span className="live-indicator" /> LIVE</span>
          <div className="profile"><div className="avatar">{user.full_name?.slice(0, 1)}</div>
          <div className="profile-text"><strong>{user.full_name}</strong>
            <span>{ROLE_LABELS[user.role]}</span></div></div>
          <button className="icon-btn" title="Безопасность учётной записи"
            aria-label="Безопасность учётной записи"
            onClick={() => setModal({ type: 'security' })}><ShieldCheck size={18} /></button>
          <button className="icon-btn" title="Выйти" onClick={logout}><LogOut size={18} /></button></div>
      </header>
      <div className="page">
        <div className="page-title"><div><div className="eyebrow">ОПЕРАЦИОННАЯ ПАНЕЛЬ / {current.label.toUpperCase()}</div>
          <h1>{current.label}</h1><p>{screen === 'dashboard'
            ? 'Ключевые показатели и события складского контура.'
            : 'Актуальные данные из транзакционной системы учёта.'}</p></div>
          <div className="page-actions"><Btn icon={RefreshCw}
            onClick={() => screen === 'admin' ? setAdminRefresh(x => x + 1) : reload()}
            disabled={loading}>Обновить</Btn></div></div>

        {screen === 'dashboard' && <>
          <div className="stats-grid">
            <Stat icon={ClipboardList} label="Открытые заказы" value={quantity(dashboard.openOrders)}
              hint="Требуют выполнения" emphasis />
            <Stat icon={ScanLine} label="Задания в работе" value={quantity(dashboard.pendingPicks)}
              hint="Ожидают отбора" />
            <Stat icon={Activity} label="Просроченные резервы" value={quantity(dashboard.overdueReservations)}
              hint="Требуют обработки worker" />
            <Stat icon={ArrowDownLeft} label="Зафиксированные недоборы"
              value={quantity(dashboard.shortages)} hint="За весь период" />
          </div>
          <div className="dashboard-grid">
            <div className="panel feature-panel"><span className="eyebrow accent">РАБОЧИЕ ПРОЦЕССЫ</span>
              <h2>Вся логистика.<br />В одном окне.</h2>
              <p>От приёмки до отгрузки: подтверждайте действия, сверяйте остатки и отслеживайте историю операций.</p>
              <div className="quick-actions"><Btn icon={Plus} variant="primary" onClick={() => openForm('order')}>
                Создать заказ</Btn><Btn icon={ScanLine} onClick={() => go('picks')}>К заданиям</Btn></div>
              <div className="feature-lines"><span /><span /><span /></div>
            </div>
            <div className="panel attention-panel"><div className="panel-title"><h3>Контроль операций</h3>
              <span className="tag">СЕГОДНЯ</span></div>
              <div className="attention-row"><div className="attention-icon"><ScanLine size={20} /></div>
                <div><strong>Комплектация</strong><span>Среднее время подтверждения</span></div>
                <strong>{dashboard.averagePickMinutes ?? 0} мин</strong></div>
              <div className="attention-row"><div className="attention-icon"><ShieldCheck size={20} /></div>
                <div><strong>Аудит движений</strong><span>Каждая проводка имеет автора и документ</span></div>
                <Check size={18} className="accent" /></div>
              <div className="attention-row"><div className="attention-icon"><Warehouse size={20} /></div>
                <div><strong>Управление складами</strong><span>Доступ определяется сервером</span></div>
                <strong>{warehouses.length}</strong></div>
            </div>
          </div>
        </>}

        {['stocks', 'products', 'receipts', 'orders', 'picks', 'shipments', 'transfers',
          'inventory', 'audit', 'anomalies'].includes(screen) && <>
          <div className="panel data-panel">
            <SectionHeader title={{
              stocks: 'Доступные остатки', anomalies: 'Контроль аномалий остатков',
              products: 'Каталог товаров',
              receipts: 'Документы приёмки', orders: 'Клиентские заказы',
              picks: 'Очередь комплектации', shipments: 'Документы отгрузки',
              transfers: 'Складские перемещения', inventory: 'Акты пересчёта',
              audit: 'История изменений'
            }[screen]} subtitle={{
              stocks: 'Физический · Резерв · Блокировка · Доступно',
              anomalies: 'Отрицательные и чрезмерно зарезервированные позиции; только диагностика',
              products: 'SKU, штрихкоды и политики FIFO / FEFO',
              receipts: 'План и факт отражаются раздельно',
              orders: 'Резервы и смена статусов',
              picks: 'Фактическое количество подтверждается сканированием',
              shipments: 'Списание и закрытие резерва — одна операция',
              transfers: 'Двойная проводка в одной SQL-транзакции',
              inventory: 'Корректировки утверждает другой сотрудник',
              audit: 'Неизменяемые события с идентификатором запроса'
            }[screen]}>
              {['stocks', 'products'].includes(screen) &&
                <Btn icon={FileSpreadsheet} onClick={downloadCsv}>Экспорт CSV</Btn>}
              {screen === 'products' && isAdmin && <Btn variant="primary" icon={Plus}
                onClick={() => openForm('product')}>Новый SKU</Btn>}
              {screen === 'receipts' && ['admin', 'manager', 'supervisor'].includes(user.role) &&
                <Btn variant="primary" icon={Plus} onClick={() => openForm('receipt')}>Новая приёмка</Btn>}
              {screen === 'orders' && canManage && <Btn variant="primary" icon={Plus}
                onClick={() => openForm('order')}>Создать заказ</Btn>}
              {screen === 'transfers' && canOperate && <Btn variant="primary" icon={Plus}
                onClick={() => openForm('transfer')}>Перемещение</Btn>}
              {screen === 'inventory' && canCount && <Btn variant="primary" icon={Plus}
                onClick={() => openForm('count')}>Новый пересчёт</Btn>}
            </SectionHeader>
            <div className="filters">
              {['stocks', 'products'].includes(screen) && <>
                <div className="search-box"><Search size={18} /><input placeholder="Поиск по SKU или названию"
                  value={search} onChange={e => setSearch(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && (setPage(1), setAppliedSearch(search))} />
                  <button onClick={() => { setAppliedSearch(search); setPage(1); }}><ChevronRight size={17} /></button></div>
              </>}
              {['stocks', 'picks', 'anomalies'].includes(screen) && <select className="warehouse-filter" value={warehouseId}
                onChange={e => setWarehouseId(e.target.value)}>
                <option value="">Все склады</option>{warehouses.map(w =>
                  <option key={w.id} value={w.id}>{w.code} / {w.name}</option>)}</select>}
              <span className="list-count">{screen === 'products' || screen === 'stocks' || screen === 'anomalies'
                ? quantity(total) + ' записей' : rows.length + ' записей'}</span>
            </div>
            {screen === 'picks'
              ? <>
                <div className="pick-filter">
                  <label htmlFor="pick-status-filter">Показывать задания</label>
                  <select id="pick-status-filter" value={pickFilter}
                    onChange={e => setPickFilter(e.target.value)}>
                    <option value="active">Ожидают / недобор</option>
                    <option value="mine">Мои активные задания</option>
                    <option value="all">Все статусы</option>
                    <option value="completed">Завершённые</option>
                  </select>
                  <span className="pick-filter-hint">
                    {rows.filter(task => ['pending', 'shortage_pending'].includes(task.status)).length}
                    {' '}активных задач · Черновики сохраняются только в этой вкладке браузера
                  </span>
                </div>
                <div className="picks-grid">{
                  rows.filter(task => pickFilter === 'all'
                    || (pickFilter === 'mine' && task.assigned_to === user?.id
                      && ['pending', 'shortage_pending'].includes(task.status))
                    || (pickFilter === 'active' && ['pending', 'shortage_pending'].includes(task.status))
                    || (pickFilter === 'completed' && ['completed', 'accepted'].includes(task.status))
                  ).length
                  ? rows.filter(task => pickFilter === 'all'
                    || (pickFilter === 'mine' && task.assigned_to === user?.id
                      && ['pending', 'shortage_pending'].includes(task.status))
                    || (pickFilter === 'active' && ['pending', 'shortage_pending'].includes(task.status))
                    || (pickFilter === 'completed' && ['completed', 'accepted'].includes(task.status))
                  ).map(task =>
                    <PickCard key={task.id} task={task} busy={busy}
                      currentUserId={user.id} assignees={pickAssignees}
                      canConfirm={canOperate && (!task.assigned_to
                        || task.assigned_to === user.id || isAdmin)}
                      canApprove={canCount} canAssign={canCount}
                      onAssign={(id, pickerId) => action('POST', '/picks/' + id + '/assign',
                        { userId: pickerId }, false, 'Исполнитель назначен')}
                      onApprove={id => action('POST', '/picks/' + id + '/approve-shortage', {},
                        false, 'Недобор согласован')}
                      onConfirm={async (id, barcode, actualQty) => {
                        const result = await action('POST', '/picks/' + id + '/confirm', { barcode, actualQty });
                        return !!result;
                      }} />)
                  : <Empty message="Заданий в выбранном статусе нет" />}</div></>

              : <div className="table-wrapper"><table><thead><tr>
                  {({
                    stocks: ['Товар', 'Склад', 'Физически', 'Резерв', 'Блок', 'Доступно'],
                    anomalies: ['SKU', 'Склад', 'Ячейка / партия', 'Физически',
                      'Резерв / Блок', 'Доступно', 'Отклонение'],
                    products: ['SKU', 'Наименование', 'Категория', 'Политика', 'Состояние'],
                    receipts: ['Документ', 'Создан', 'Статус', 'Действия'],
                    orders: ['Заказ', 'Клиент', 'Создан', 'Статус', 'Действия'],
                    shipments: ['Отгрузка', 'Заказ', 'Склад', 'Статус', 'Действия'],
                    transfers: ['Перемещение', 'Количество', 'Статус', 'Действия'],
                    inventory: ['Акт', 'Зона', 'Дата', 'Статус', 'Действия'],
                    audit: ['Событие', 'Объект', 'Автор', 'Дата', 'Request ID']
                  }[screen] || []).map(name => <th key={name}>{name}</th>)}</tr></thead>
                  <tbody>{rows.map((r, i) => <tr key={r.id || i}>
                    {screen === 'stocks' && <>
                      <td><strong>{r.sku}</strong><small>{r.name}</small></td><td>{r.warehouse}</td>
                      <td>{quantity(r.physical)}</td><td>{quantity(r.reserved)}</td>
                      <td>{quantity(r.blocked)}</td><td><strong className={r.available <= 0 ? 'negative' : 'positive'}>{quantity(r.available)}</strong></td></>}
                    {screen === 'anomalies' && <>
                      <td><strong>{r.sku}</strong></td><td>{r.warehouse}</td>
                      <td><strong>{r.bin}</strong><small>{r.lot}</small></td>
                      <td>{r.physical}</td><td>{r.reserved} / {r.blocked}</td>
                      <td><strong className="negative">{r.available}</strong></td>
                      <td><span className="status danger">{r.issue === 'NEGATIVE_PHYSICAL'
                        ? 'Отрицательный остаток' : 'Превышен доступный остаток'}</span></td></>}
                    {screen === 'products' && <>
                      <td><strong>{r.sku}</strong><small>{r.barcode || 'Нет штрихкода'}</small></td>
                      <td>{r.name}</td><td>{r.category}</td><td><span className="tag">{r.pick_policy}</span></td>
                      <td>{r.active ? <span className="positive">Активен</span> : <span className="negative">Архивный</span>}</td></>}
                    {screen === 'receipts' && <>
                      <td><strong>RC-{short(r.id)}</strong></td><td>{date(r.created_at)}</td>
                      <td><Pill status={r.status} /></td><td>{r.status === 'draft' && canOperate &&
                        <Btn icon={Check} disabled={busy} onClick={() => openForm('receipt-confirm', r)}>Принять</Btn>}</td></>}
                    {screen === 'orders' && <>
                      <td><strong>OR-{short(r.id)}</strong><small>v{r.version}</small></td><td>{r.customer}</td>
                      <td>{date(r.created_at)}</td><td><Pill status={r.status} /></td>
                      <td><div className="table-actions">{r.status === 'draft' && canManage &&
                        <Btn disabled={busy} onClick={() => action('POST', '/orders/' + r.id + '/reserve', { ttlMinutes: 60 }, true, 'Резерв подтверждён')}>Резерв</Btn>}
                        <Btn onClick={async () => { const d = await api('GET', '/orders/' + r.id); setDetail(d); setModal({ type: 'order-detail', item: r }); }}>Подробнее</Btn></div></td></>}
                    {screen === 'shipments' && <>
                      <td><strong>SH-{short(r.id)}</strong></td><td>{short(r.order_id)}</td>
                      <td>{short(r.warehouse_id)}</td><td><Pill status={r.status} /></td>
                      <td><div className="table-actions">{r.status === 'draft' && canOperate &&
                        <Btn disabled={busy} onClick={() => action('POST', '/shipments/' + r.id + '/confirm', {}, true)}>Отгрузить</Btn>}
                        {r.status === 'posted' && canOperate &&
                          <Btn onClick={() => openForm('return', r)}>Возврат</Btn>}</div></td></>}
                    {screen === 'transfers' && <>
                      <td><strong>TR-{short(r.id)}</strong></td><td>{r.qty}</td><td><Pill status={r.status} /></td>
                      <td>{r.status === 'draft' && canOperate &&
                        <Btn disabled={busy} onClick={() => action('POST', '/transfers/' + r.id + '/confirm', {}, true)}>Провести</Btn>}</td></>}
                    {screen === 'inventory' && <>
                      <td><strong>IC-{short(r.id)}</strong></td><td>{r.zone_code}</td>
                      <td>{date(r.created_at)}</td><td><Pill status={r.status} /></td>
                      <td><div className="table-actions">
                        <Btn onClick={() => openForm('inventory-variance', r)}>Расхождения</Btn>
                        {r.status === 'counting' && canOperate &&
                        <Btn onClick={() => openForm('inventory-count', r)}>Внести факт</Btn>}
                        {r.status === 'counting' && canOperate &&
                          <Btn disabled={busy} onClick={() => action('POST', '/inventory/' + r.id + '/submit', {})}>Отправить</Btn>}
                        {r.status === 'submitted' && canCount &&
                          <Btn disabled={busy} onClick={() => action('POST', '/inventory/' + r.id + '/approve', {})}>Утвердить</Btn>}</div></td></>}
                    {screen === 'audit' && <>
                      <td><strong>{r.action}</strong></td><td>{r.entity_type}<small>{short(r.entity_id)}</small></td>
                      <td>{r.actor || 'Системный процесс'}</td><td>{date(r.created_at)}</td>
                      <td><code>{short(r.request_id)}</code></td></>}
                  </tr>)}</tbody></table>
                {!rows.length && <Empty />}</div>}
            {['products', 'stocks'].includes(screen) && total > 30 && <div className="pagination">
              <Btn disabled={page <= 1} onClick={() => setPage(page - 1)}>Назад</Btn><span>Страница {page}</span>
              <Btn disabled={page * (screen === 'products' ? 30 : 50) >= total}
                onClick={() => setPage(page + 1)}>Вперёд</Btn></div>}
          </div>
        </>}

        {screen === 'admin' && isAdmin && <AdminConsole api={api} warehouses={warehouses}
          currentUserId={user.id} refreshToken={adminRefresh}
          onNotify={alertMessage} onWarehousesChanged={async () => {
            const next = await api('GET', '/warehouses');
            setWarehouses(next);
          }} />}

        {screen === 'import' && <div className="panel import-panel">
          <FileSpreadsheet size={38} className="accent" /><h2>Импорт номенклатуры из CSV</h2>
          <p>Проверка дублирующихся SKU и построчных ошибок перед сохранением. Импорт полностью атомарный.</p>
          <textarea className="csv-input" placeholder={'sku,name,unit,barcode,category,pick_policy\nSKU-00001,Гайка,шт,4600000000001,Метизы,FIFO'}
            value={form.csv || ''} onChange={e => set('csv', e.target.value)} />
          <input type="file" accept=".csv,text/csv" onChange={async e => {
            const file = e.target.files?.[0];
            if (file) set('csv', await file.text());
          }} />
          <div className="quick-actions"><Btn icon={Search} onClick={async () => {
            const result = await action('POST', '/import/products/preview', { content: form.csv || '' });
            if (result) setDetail(result);
          }}>Предпросмотр</Btn><Btn variant="primary" icon={Check} disabled={!detail?.valid}
            onClick={() => action('POST', '/import/products/commit', { content: form.csv || '' })}>Импортировать</Btn></div>
          {detail?.errors && <div className="csv-report"><strong>Строк: {detail.count}. Ошибок: {detail.errors.length}</strong>
            {detail.errors.slice(0, 20).map((err, i) => <div key={i}>Строка {err.row}: {err.message}</div>)}</div>}
        </div>}
        <footer className="footer"><span>© StockPilot · Transactional WMS</span>
          <span>Все складские движения требуют подтверждения сервером</span></footer>
      </div>
    </main>
    {notice && <div className="toast"><Activity size={16} />{notice}<button onClick={() => setNotice('')}><X size={15} /></button></div>}
    {modal && <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && setModal(null)}>
      <div className="modal"><div className="modal-header"><div><span className="eyebrow accent">СКЛАДСКАЯ ОПЕРАЦИЯ</span>
        <h2>{({
          product: 'Новый товар', receipt: 'Создать приёмку', order: 'Новый заказ',
          transfer: 'Новое перемещение', count: 'Инвентаризация',
          return: 'Возврат отгрузки', 'receipt-confirm': 'Подтверждение приёмки',
          'inventory-count': 'Внести фактический остаток',
          'inventory-variance': 'Сводка расхождений', 'order-detail': 'Детали заказа',
          security: 'Безопасность учётной записи'
        }[modal.type])}</h2></div><button className="icon-btn" onClick={() => setModal(null)}><X size={20} /></button></div>
        {modal.type === 'inventory-variance' ? <div className="modal-body">
          {!detail ? <p>Загрузка акта…</p> : <>
            <div className="variance-kpis">
              <div><span>Расхождения</span><strong>{detail.discrepancyLines}</strong></div>
              <div><span>Разница, шт.</span><strong>{detail.deltaQty}</strong></div>
              <div><span>Не пересчитано</span><strong>{detail.unentered}</strong></div>
            </div>
            {detail.staleSnapshot && <div className="error-note">
              После создания акта в зоне появились движения. Утверждение будет отклонено.
              Необходимо провести новый пересчёт.</div>}
            {detail.items.map(line => <div className="detail-row" key={line.id}>
              <span>{line.sku} · {line.bin} / {line.lot}</span>
              <strong>{line.snapshot_qty} → {line.actual_qty ?? '—'}
                {line.delta === null ? '' : ' (' + (line.delta > 0 ? '+' : '') + line.delta + ')'}</strong>
            </div>)}
            {!detail.items.length && <p>В зоне пока нет учётных строк.</p>}
            <div className="modal-buttons"><Btn onClick={() => setModal(null)}>Закрыть</Btn></div>
          </>}
        </div> : modal.type === 'order-detail' ? <div className="modal-body">
          <div className="detail-meta">Статус: <Pill status={detail?.order?.status} /></div>
          <h3>Позиции заказа</h3>{detail?.items?.map(l => <div className="detail-row" key={l.id}>
            <span>{l.sku} · {l.warehouse}</span><strong>{l.qty} шт.</strong></div>)}
          <h3>Задания</h3>{detail?.picks?.map(t => <div className="detail-row" key={t.id}>
            <span>{t.sku} / {t.bin_code}</span><span><Pill status={t.status} /> {t.actual_qty ?? 0}/{t.expected_qty}</span></div>)}
          <h3>Отгрузки</h3>{detail?.shipments?.map(s => <div className="detail-row" key={s.id}>
            <span>{short(s.id)}</span><Pill status={s.status} /></div>)}
          {canManage && !['shipped','cancelled'].includes(detail?.order?.status) &&
            <div className="modal-buttons"><Btn disabled={busy} onClick={async () => {
              if (!window.confirm('Отменить заказ и освободить активные резервы?')) return;
              const result = await action('POST','/orders/' + detail.order.id + '/cancel', {});
              if (result) setModal(null);
            }}>Отменить заказ</Btn></div>}
        </div> : modal.type === 'order' ?
          <OrderComposer api={api} warehouses={warehouses} busy={busy}
            onCreate={async payload => {
              const result = await action('POST', '/orders', payload);
              if (result) setModal(null);
              return result;
            }} />
        : modal.type === 'return' ?
          <ReturnComposer api={api} shipment={modal.item} busy={busy}
            onCreate={async payload => {
              const result = await action('POST', '/shipments/' + modal.item.id + '/returns',
                payload, true, 'Возврат проведён');
              if (result) setModal(null);
              return result;
            }} />
        : modal.type === 'security' ?
          <SecurityComposer api={api} busy={busy} onRevoke={message => {
            logout();
            alertMessage(message);
          }} />
        : <form onSubmit={submitModal} className="modal-body">
          {modal.type === 'product' && <>{inputField('Артикул SKU', 'sku')}
            {inputField('Название', 'name')}{inputField('Штрихкод', 'barcode', 'text', false)}
            {inputField('Категория', 'category', 'text', false)}
            {selectField('Политика отбора', 'pickPolicy', [
              { value: 'FIFO', label: 'FIFO — по дате прихода' },
              { value: 'FEFO', label: 'FEFO — по сроку годности' }
            ])}</>}
          {['receipt', 'transfer', 'count'].includes(modal.type) && whField()}
          {['receipt', 'transfer'].includes(modal.type) && productField()}
          {['receipt', 'transfer'].includes(modal.type) && <>{binField()}{lotField()}</>}
          {modal.type === 'transfer' && binField('toBinId', 'Целевая ячейка')}
          {['receipt', 'transfer'].includes(modal.type) &&
            inputField('Количество', 'qty', 'number')}

          {modal.type === 'count' && selectField('Зона пересчёта', 'zoneId',
            zoneOptions.map(z => ({ value: z.id, label: z.code + ' / ' + z.name })))}

          {modal.type === 'receipt-confirm' && detail && detail.items.map((item, i) => <div key={item.id} className="count-line">
            <strong>{item.sku}</strong><small>План: {item.planned_qty}</small>
            <label className="field"><span>Принято фактически</span><input type="number" min="0"
              value={form.items?.[i]?.actualQty ?? 0}
              onChange={e => setForm(prev => ({ ...prev, items: prev.items.map((r, j) =>
                i === j ? { ...r, actualQty: Number(e.target.value) } : r) }))} /></label>
            <label className="field"><span>Причина расхождения</span><input
              value={form.items?.[i]?.reason || ''}
              onChange={e => setForm(prev => ({ ...prev, items: prev.items.map((r, j) =>
                i === j ? { ...r, reason: e.target.value } : r) }))} /></label>
          </div>)}
          {modal.type === 'inventory-count' && detail && detail.items.map((item, i) =>
            <div key={item.id} className="count-line"><strong>{item.sku} · {item.bin_code}</strong>
              <small>Учётный срез: {item.snapshot_qty}</small>
              <label className="field"><span>Факт</span><input type="number" min="0"
                value={form.items?.[i]?.actualQty ?? ''}
                onChange={e => setForm(prev => ({ ...prev, items: prev.items.map((r, j) =>
                  i === j ? { ...r, actualQty: Number(e.target.value) } : r) }))} /></label>
            </div>)}
          <div className="modal-note">Операция будет проверена сервером. Неподтверждённые данные
            не изменяют складской остаток.</div>
          <div className="modal-buttons"><Btn onClick={() => setModal(null)}>Отмена</Btn>
            <Btn variant="primary" type="submit" disabled={busy}><Check size={16} />Сохранить</Btn></div>
        </form>}</div>
    </div>}
  </div>;
}
