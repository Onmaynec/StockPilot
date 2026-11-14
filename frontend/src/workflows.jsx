import React, { useEffect, useState } from 'react';
import { Check, Plus, Search, Trash2, ShieldCheck } from 'lucide-react';

/**
 * Searchable SKU selection, with one warehouse on every individual line.
 * The server remains responsible for validating every line and reservation.
 */
export function OrderComposer({ api, warehouses, onCreate, busy }) {
  const [customer, setCustomer] = useState('');
  const [query, setQuery] = useState('');
  const [products, setProducts] = useState([]);
  const [chosen, setChosen] = useState('');
  const [lines, setLines] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const handle = setTimeout(async () => {
      try {
        const result = await api('GET', '/products?q=' + encodeURIComponent(query) + '&limit=40&active=true');
        if (!cancelled) {
          setProducts(result.items || []);
          setChosen(prev => (result.items || []).some(p => p.id === prev)
            ? prev : (result.items?.[0]?.id || ''));
          setError('');
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [query, api]);

  function addLine() {
    if (lines.length >= 50) { setError('В заказе не более 50 позиций.'); return; }
    const product = products.find(row => row.id === chosen);
    if (!product) return;
    setLines(prev => [...prev, {
      key: crypto.randomUUID(), productId: product.id, sku: product.sku,
      name: product.name, warehouseId: warehouses[0]?.id || '', qty: 1
    }]);
  }
  function updateLine(key, patch) {
    setLines(prev => prev.map(line => line.key === key ? { ...line, ...patch } : line));
  }
  async function submit(e) {
    e.preventDefault();
    if (!customer.trim() || !lines.length) {
      setError('Укажите контрагента и добавьте хотя бы одну позицию.');
      return;
    }
    if (lines.some(line => !line.warehouseId || !Number.isInteger(Number(line.qty)) || Number(line.qty) < 1)) {
      setError('Проверьте склад и количество в каждой строке.');
      return;
    }
    const result = await onCreate({
      customer: customer.trim(),
      items: lines.map(({ productId, warehouseId, qty }) => ({
        productId, warehouseId, qty: Number(qty)
      }))
    });
    if (!result) setError('Заказ не создан. Проверьте сообщение об ошибке.');
  }

  return <form className="modal-body" onSubmit={submit} data-testid="order-composer">
    <label className="field"><span>Контрагент</span>
      <input value={customer} onChange={e => setCustomer(e.target.value)}
        placeholder="Например, ООО Комплект" required maxLength={250} /></label>
    <div className="composer-chooser">
      <label className="field"><span>Поиск товара по артикулу или названию</span>
        <div className="input-with-icon"><Search size={18} />
          <input value={query} onChange={e => setQuery(e.target.value)}
            placeholder="Например, SKU-00001" aria-label="Поиск товара" /></div></label>
      <label className="field"><span>{loading ? 'Загрузка товаров…' : 'Выбранный товар'}</span>
        <select aria-label="Выбранный товар" value={chosen}
          onChange={e => setChosen(e.target.value)}>
          {products.length ? products.map(product => <option key={product.id} value={product.id}>
            {product.sku} — {product.name}</option>)
            : <option value="">Нет совпадений</option>}
        </select></label>
      <button type="button" className="btn secondary" onClick={addLine}
        disabled={!chosen || !warehouses.length || lines.length >= 50}><Plus size={16} />Добавить позицию</button>
    </div>
    <div className="composer-heading"><strong>Позиции заказа</strong>
      <span>{lines.length} из 50</span></div>
    {lines.length ? lines.map((line, i) => <div className="composer-line" key={line.key}
      data-testid="order-line">
      <div className="composer-line-top"><strong>{i + 1}. {line.sku}</strong>
        <button type="button" className="icon-btn" title="Удалить позицию"
          onClick={() => setLines(prev => prev.filter(item => item.key !== line.key))}>
          <Trash2 size={17} /></button></div>
      <small>{line.name}</small>
      <div className="composer-fields">
        <label className="field"><span>Склад</span>
          <select aria-label={'Склад строки ' + (i + 1)} value={line.warehouseId}
            onChange={e => updateLine(line.key, { warehouseId: e.target.value })} required>
            {warehouses.map(wh => <option key={wh.id} value={wh.id}>
              {wh.code} / {wh.name}</option>)}
          </select></label>
        <label className="field"><span>Количество</span><input type="number" min="1" step="1"
          aria-label={'Количество строки ' + (i + 1)}
          value={line.qty} onChange={e => updateLine(line.key, { qty: e.target.value })}
          required /></label>
      </div>
    </div>) : <p className="composer-empty">Добавьте товары из каталога. Для каждой позиции можно выбрать свой склад.</p>}
    {error && <div className="error-note">{error}</div>}
    <p className="helper">Резервирование выполняется отдельно, после создания заказа. При нехватке
      хотя бы одной позиции операция целиком отклоняется.</p>
    <div className="modal-buttons"><button type="submit" className="btn primary"
      disabled={busy || !lines.length || lines.length > 50}>
      <Check size={16} />Создать заказ</button></div>
  </form>;
}

export function ReturnComposer({ api, shipment, onCreate, busy }) {
  const [positions, setPositions] = useState([]);
  const [bins, setBins] = useState([]);
  const [reservationId, setReservationId] = useState('');
  const [binId, setBinId] = useState('');
  const [qty, setQty] = useState(1);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api('GET', '/shipments/' + shipment.id + '/returnable'),
      api('GET', '/bins?warehouseId=' + encodeURIComponent(shipment.warehouse_id))
    ]).then(([items, locations]) => {
      if (cancelled) return;
      setPositions(items.items);
      setBins(locations.filter(row => row.active));
      setReservationId(items.items[0]?.reservation_id || '');
      setBinId(locations.find(row => row.active)?.id || '');
    }).catch(err => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, shipment.id, shipment.warehouse_id]);

  const selected = positions.find(p => p.reservation_id === reservationId);
  async function submit(e) {
    e.preventDefault();
    if (!selected || !binId || qty < 1 || qty > selected.returnable_qty) {
      setError('Укажите корректное количество и ячейку возврата.');
      return;
    }
    const result = await onCreate({ reservationId, binId, qty: Number(qty) });
    if (!result) setError('Возврат не проведён; проверьте сообщение об ошибке.');
  }

  return <form className="modal-body" onSubmit={submit} data-testid="return-composer">
    {loading ? <p>Загрузка позиций отгрузки…</p> : positions.length ? <>
      <label className="field"><span>Отгруженная позиция</span>
        <select value={reservationId} onChange={e => { setReservationId(e.target.value); setQty(1); }}
          aria-label="Позиция возврата">
          {positions.map(p => <option key={p.reservation_id} value={p.reservation_id}>
            {p.sku} — {p.name} (доступно к возврату: {p.returnable_qty})</option>)}
        </select></label>
      <div className="composer-line"><div className="composer-line-top">
        <strong>Можно вернуть: {selected?.returnable_qty || 0} шт.</strong></div>
        <small>Отгружено: {selected?.shipped_qty ?? 0}; уже возвращено: {selected?.returned_qty ?? 0}</small></div>
      <label className="field"><span>Приёмная ячейка</span>
        <select value={binId} onChange={e => setBinId(e.target.value)}
          aria-label="Ячейка возврата" required>
          {bins.map(bin => <option key={bin.id} value={bin.id}>{bin.zone_code} / {bin.code}</option>)}
        </select></label>
      <label className="field"><span>Фактически возвращено</span><input type="number"
        min="1" max={selected?.returnable_qty || 0} step="1" value={qty}
        aria-label="Количество возврата" onChange={e => setQty(Number(e.target.value))} required /></label>
      <div className="modal-note">Возврат увеличит физический остаток, но не создаст
        новый резерв для завершённого заказа.</div>
      <div className="modal-buttons"><button className="btn primary" type="submit"
        disabled={busy || !selected || !binId || qty < 1 || qty > selected.returnable_qty}>
        <Check size={16} />Провести возврат</button></div>
    </> : <div className="empty">По этой отгрузке больше нечего возвращать.</div>}
    {error && <div className="error-note">{error}</div>}
  </form>;
}

export function SecurityComposer({ api, onRevoke, busy }) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [message, setMessage] = useState('');
  const [working, setWorking] = useState(false);
  async function rotate(e) {
    e.preventDefault();
    setWorking(true);
    try {
      await api('POST', '/auth/change-password', { currentPassword, newPassword });
      onRevoke('Пароль изменён. Войдите снова.');
    } catch (err) { setMessage(err.message); }
    finally { setWorking(false); }
  }
  async function revoke() {
    if (!window.confirm('Завершить все сеансы этого пользователя?')) return;
    setWorking(true);
    try {
      await api('POST', '/auth/logout-all', {});
      onRevoke('Сеансы отозваны. Войдите снова.');
    } catch (err) { setMessage(err.message); }
    finally { setWorking(false); }
  }
  return <div className="modal-body">
    <div className="security-heading"><ShieldCheck size={22} />
      <div><strong>Управление входом</strong><p>Смена пароля завершит все действующие сеансы.</p></div></div>
    <form onSubmit={rotate}>
      <label className="field"><span>Текущий пароль</span><input type="password"
        autoComplete="current-password" value={currentPassword}
        onChange={e => setCurrentPassword(e.target.value)} required /></label>
      <label className="field"><span>Новый пароль (не меньше 12 символов)</span><input type="password"
        autoComplete="new-password" minLength={12} maxLength={128} value={newPassword}
        onChange={e => setNewPassword(e.target.value)} required /></label>
      <button type="submit" className="btn primary" disabled={working || busy}>
        Изменить пароль</button>
    </form>
    <hr className="security-divider" />
    <p className="helper">Отозвать все сеансы без смены пароля.</p>
    <button className="btn secondary" type="button" onClick={revoke}
      disabled={working || busy}>Выйти со всех устройств</button>
    {message && <div className="error-note">{message}</div>}
  </div>;
}
