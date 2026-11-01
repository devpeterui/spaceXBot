/* Phase 2: all Deriv contract types for the Deriv market. Additive panel. */
(function () {
  'use strict';
  if (window.__vtDerivs) return;
  var g = function (id) { return document.getElementById(id); };
  var st = { cat: [], cur: null, busy: false, mk: '', loaded: false, loadingCat: false };

  var css = document.createElement('style');
  css.textContent =
    '.vd{text-align:left;margin:0 0 14px;background:#2a2a2a;border-radius:8px;padding:12px}' +
    '.vd select,.vd input{margin:0 0 8px;padding:8px;font-size:14px}' +
    '.vd .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:0 8px}' +
    '.vd .row button{padding:10px;font-size:13px}' +
    '.vd .hint{font-size:11px;color:#999;margin:2px 0 8px;line-height:1.4}' +
    '.vd .exp{font-size:11px;color:#ffb74d;margin-bottom:8px}' +
    '.vd .msg{font-size:12px;margin-top:8px;word-break:break-word;color:#ccc}';
  document.head.appendChild(css);

  var box = document.createElement('div');
  box.className = 'vd hidden';
  box.innerHTML =
    '<label for="dvType">Derivatives: contract type</label><select id="dvType"></select>' +
    '<label for="dvMkt">Market (only those that support this contract)</label><select id="dvMkt"></select>' +
    '<div class="exp hidden" id="dvExp">Experimental: strike/barrier rules differ per market. If Deriv refuses, the message is shown below.</div>' +
    '<div class="grid">' +
    '<div data-f="time"><label for="dvDur">Duration</label><input id="dvDur" type="number" min="1" step="1" value="5"></div>' +
    '<div data-f="time"><label for="dvUnit">Unit</label><select id="dvUnit"><option value="m">minutes</option><option value="s">seconds</option><option value="h">hours</option><option value="t">ticks</option></select></div>' +
    '<div data-f="ticks"><label for="dvTicks">Ticks (1-10)</label><input id="dvTicks" type="number" min="1" max="10" step="1" value="5"></div>' +
    '<div data-f="barrier"><label for="dvBar">Barrier offset (points)</label><input id="dvBar" type="number" step="any" value="0.5"></div>' +
    '<div data-f="digit"><label for="dvDigit">Digit (0-9)</label><input id="dvDigit" type="number" min="0" max="9" step="1" value="5"></div>' +
    '<div data-f="growth_rate"><label for="dvGrowth">Growth rate (0.01-0.05)</label><input id="dvGrowth" type="number" min="0.01" max="0.05" step="0.01" value="0.01"></div>' +
    '<div data-f="multiplier"><label for="dvMult">Multiplier</label><input id="dvMult" type="number" min="1" step="1" value="10"></div>' +
    '<div data-f="stop_loss"><label for="dvSL">Stop loss (amount)</label><input id="dvSL" type="number" min="0" step="any" value="0.5"></div>' +
    '<div data-f="take_profit"><label for="dvTP">Take profit (amount)</label><input id="dvTP" type="number" min="0" step="any" value="1"></div></div>' +
    '<div class="hint" id="dvHint"></div>' +
    '<div class="row"><button type="button" id="dvUp"></button><button type="button" id="dvDown" class="sell"></button></div>' +
    '<div class="msg" id="dvMsg">Uses the stake from the form above.</div>';

  function call(path, opt) {
    opt = opt || {};
    opt.headers = Object.assign({
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + (sessionStorage.getItem('token') || '')
    }, opt.headers || {});
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
    });
  }
  function msg(t, color) { var m = g('dvMsg'); m.textContent = t; m.style.color = color || '#ccc'; }
  function num(id) { return parseFloat(g(id).value); }

  function params() {
    return {
      duration: parseInt(g('dvDur').value, 10) || 5, unit: g('dvUnit').value,
      ticks: parseInt(g('dvTicks').value, 10) || 5, barrier: num('dvBar'), digit: parseInt(g('dvDigit').value, 10),
      growth_rate: num('dvGrowth'), multiplier: num('dvMult'), stop_loss: num('dvSL'), take_profit: num('dvTP')
    };
  }

  function current() { return st.cat.filter(function (c) { return c.id === g('dvType').value; })[0]; }

  function showFields() {
    var c = current(); st.cur = c;
    if (!c) return;
    var want = {};
    c.fields.forEach(function (f) { want[f] = 1; });
    if (c.dur === 'time') want.time = 1;
    if (c.dur === 'ticks') want.ticks = 1;
    box.querySelectorAll('[data-f]').forEach(function (d) { d.classList.toggle('hidden', !want[d.getAttribute('data-f')]); });
    var up = c.actions.up, dn = c.actions.down;
    g('dvUp').textContent = up.label;
    g('dvDown').textContent = dn ? dn.label : '';
    g('dvDown').classList.toggle('hidden', !dn);
    g('dvHint').textContent = up.label + ': ' + up.hint + (dn ? '.  ' + dn.label + ': ' + dn.hint : '') + '.';
    g('dvExp').classList.toggle('hidden', !c.experimental);
    if (c.id === 'vanilla' || c.id === 'turbos') g('dvBar').min = '';
    loadMarkets();
  }

  function loadMarkets() {
    var c = current();
    if (!c) return;
    var sel = g('dvMkt'), keep = (g('symbol') || {}).value;
    sel.innerHTML = '<option>Loading markets...</option>';
    call('/derivs/markets?contract=' + encodeURIComponent(c.id)).then(function (r) {
      sel.innerHTML = '';
      if (!r.ok || r.data.status !== 'success') {
        sel.innerHTML = '<option value="">' + ((r.data && r.data.message) || 'Could not load markets') + '</option>';
        return;
      }
      var groups = {};
      r.data.markets.forEach(function (m) {
        var og = groups[m.market];
        if (!og) { og = groups[m.market] = document.createElement('optgroup'); og.label = m.market.replace(/_/g, ' '); sel.appendChild(og); }
        var o = document.createElement('option');
        o.value = m.symbol; o.textContent = m.name + ' (' + m.symbol + ')'; og.appendChild(o);
      });
      var hasKeep = r.data.markets.some(function (m) { return m.symbol === keep; });
      if (hasKeep) sel.value = keep; else if (r.data.markets.length) pickMarket();
    }).catch(function () { sel.innerHTML = '<option value="">Cannot reach the server</option>'; });
  }

  function pickMarket() {
    var v = g('dvMkt').value, s = g('symbol');
    if (!v || !s) return;
    s.value = v;
    s.dispatchEvent(new Event('change'));   // the chart reloads for the new market
  }

  function fire(action) {
    var c = current();
    if (!c || st.busy) return;
    var symbol = ((g('symbol') || {}).value || '').trim(), stake = parseFloat((g('volume') || {}).value);
    if (!symbol || !(stake > 0)) { msg('Enter a market and a stake in the form above.', '#ff5252'); return; }
    var label = c.actions[action].label;
    if (typeof isDemo !== 'undefined' && !isDemo && !confirm('LIVE ACCOUNT: ' + label + ' ' + stake + ' on ' + symbol + '?')) return;
    st.busy = true; g('dvUp').disabled = g('dvDown').disabled = true; msg('Sending ' + label + '...', '#ffeb3b');
    call('/derivs/execute', { method: 'POST', body: JSON.stringify(Object.assign(params(), { contract: c.id, action: action, symbol: symbol, stake: stake })) })
      .then(function (r) {
        if (r.status === 401) { msg('Session expired, log in again.', '#ff5252'); return; }
        if (r.data && r.data.status === 'success') {
          msg(label + ' placed. Order #' + r.data.order + ' @ ' + r.data.price + (r.data.longcode ? '. ' + r.data.longcode : ''), '#00e676');
          if (typeof window.refreshAccount === 'function') { try { window.refreshAccount().catch(function () {}); } catch (e) {} }
        } else msg('Failed: ' + ((r.data && r.data.message) || r.status), '#ff5252');
      }).catch(function () { msg('Cannot reach the server.', '#ff5252'); })
      .then(function () { st.busy = false; g('dvUp').disabled = g('dvDown').disabled = false; });
  }

  function loadCatalog() {
    if (st.loaded || st.loadingCat) return;
    st.loadingCat = true;
    call('/derivs/catalog').then(function (r) {
      if (r.ok && r.data.status === 'success') {
        st.cat = r.data.contracts; st.loaded = true;
        var sel = g('dvType'); sel.innerHTML = '';
        st.cat.forEach(function (c) { var o = document.createElement('option'); o.value = c.id; o.textContent = c.label; sel.appendChild(o); });
        showFields();
      }
    }).catch(function () {}).then(function () { st.loadingCat = false; });
  }

  var mounted = false;
  function mount() {
    if (mounted) return true;
    var chat = document.querySelector('.chat');
    if (!chat || !chat.parentNode) return false;
    chat.parentNode.insertBefore(box, chat);
    mounted = true;
    g('dvType').addEventListener('change', showFields);
    g('dvMkt').addEventListener('change', pickMarket);
    g('dvUp').addEventListener('click', function () { fire('up'); });
    g('dvDown').addEventListener('click', function () { fire('down'); });
    return true;
  }

  window.__vtDerivs = { params: params, catalog: function () { return st.cat; } };

  setInterval(function () {
    if (!mount()) return;
    var view = g('tradeView');
    var visible = !!view && !view.classList.contains('hidden') && sessionStorage.getItem('broker') === 'deriv';
    box.classList.toggle('hidden', !visible);
    if (visible) loadCatalog();
  }, 1500);
})();
