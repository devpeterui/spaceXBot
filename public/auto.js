/* Phase 1: MANUAL / AUTO switch for the Deriv market. Additive panel. */
(function () {
  'use strict';
  if (window.__vtAuto) return;
  window.__vtAuto = true;
  var g = function (id) { return document.getElementById(id); };
  var st = { running: false, busy: false, t: 0, log: [] };

  var css = document.createElement('style');
  css.textContent =
    '.va{text-align:left;margin:0 0 14px;background:#2a2a2a;border-radius:8px;padding:12px}' +
    '.va button{width:auto;padding:8px 14px;font-size:13px;background:#3a3a3a;color:#fff;font-weight:normal}' +
    '.va button:hover{background:#4a4a4a}' +
    '.va .seg{display:flex;gap:6px;margin-bottom:10px}.va .seg button{flex:1}' +
    '.va button.man.on{background:#455a64;font-weight:bold}' +
    '.va button.aut.on{background:#00e676;color:#121212;font-weight:bold}' +
    '.va .set{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}' +
    '.va .set input{margin:0;padding:8px;font-size:14px}' +
    '.va .info{font-size:12px;color:#bbb;margin:8px 0}' +
    '.va .log{max-height:150px;overflow:auto;font-size:11px;color:#aaa;line-height:1.5;border-top:1px solid #333;padding-top:6px}' +
    '.va .warn{font-size:11px;color:#888;margin-top:6px}';
  document.head.appendChild(css);

  var box = document.createElement('div');
  box.className = 'va hidden';
  box.innerHTML =
    '<div class="seg"><button type="button" class="man on" id="vaMan">MANUAL</button>' +
    '<button type="button" class="aut" id="vaAut">AUTO</button></div>' +
    '<div class="set">' +
    '<div><label for="vaConf">Min confidence %</label><input id="vaConf" type="number" min="50" max="95" value="65"></div>' +
    '<div><label for="vaInt">Check every (sec)</label><input id="vaInt" type="number" min="60" max="3600" value="120"></div>' +
    '<div><label for="vaMax">Max trades / hour</label><input id="vaMax" type="number" min="1" max="30" value="6"></div></div>' +
    '<div class="info" id="vaInfo">Manual: you press RISE / FALL yourself.</div>' +
    '<div class="log" id="vaLog"></div>' +
    '<div class="warn">AUTO uses the market, stake and duration above plus the chart timeframe. Demo accounts only. ' +
    'It stops itself on daily limits or repeated errors. Short-term moves are close to random: expect losses too.</div>';

  var mounted = false;
  function mount() {
    if (mounted) return true;
    var chat = document.querySelector('.chat');
    if (!chat || !chat.parentNode) return false;
    chat.parentNode.insertBefore(box, chat);
    mounted = true;
    g('vaMan').addEventListener('click', function () { if (st.running) toggle(false); });
    g('vaAut').addEventListener('click', function () { if (!st.running) toggle(true); });
    return true;
  }

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

  function settings() {
    var tf = g('vtTf');
    return {
      symbol: (g('symbol') || {}).value || '',
      volume: parseFloat((g('volume') || {}).value),
      duration: parseInt((g('duration') || {}).value, 10) || 5,
      granularity: tf ? parseInt(tf.value, 10) : 60,
      min_confidence: parseInt(g('vaConf').value, 10) || 65,
      interval: parseInt(g('vaInt').value, 10) || 120,
      max_per_hour: parseInt(g('vaMax').value, 10) || 6
    };
  }

  function hhmmss(sec) {
    var d = new Date(sec * 1000), p = function (n) { return ('0' + n).slice(-2); };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function render(d) {
    st.running = !!d.running;
    g('vaMan').classList.toggle('on', !st.running);
    g('vaAut').classList.toggle('on', st.running);
    ['vaConf', 'vaInt', 'vaMax'].forEach(function (id) { g(id).disabled = st.running; });
    ['symbol', 'volume', 'duration'].forEach(function (id) { if (g(id)) g(id).disabled = st.running; });
    var info = g('vaInfo');
    if (st.running) {
      info.textContent = 'AUTO is ON. Trades placed: ' + (d.placed || 0) + ' (' + (d.trades_last_hour || 0) +
        ' in the last hour), skipped: ' + (d.waits || 0) + (d.next_free ? '. Contract open for ~' + d.next_free + 's.' : '.');
    } else {
      info.textContent = 'Manual: you press RISE / FALL yourself.';
    }
    var lg = g('vaLog');
    lg.textContent = '';
    (d.log || []).slice().reverse().forEach(function (x) {
      var p = document.createElement('div');
      p.textContent = hhmmss(x.t) + '  ' + x.m;
      lg.appendChild(p);
    });
  }

  function toggle(on) {
    if (st.busy) return;
    st.busy = true;
    var p = on ? call('/auto/start', { method: 'POST', body: JSON.stringify(settings()) })
               : call('/auto/stop', { method: 'POST', body: '{}' });
    p.then(function (r) {
      if (r.data && r.data.status === 'success') render(r.data);
      else g('vaInfo').textContent = (r.data && r.data.message) || 'Could not change mode.';
    }).catch(function () { g('vaInfo').textContent = 'Cannot reach the server.'; })
      .then(function () { st.busy = false; });
  }

  function poll() {
    call('/auto/status').then(function (r) {
      if (r.data && r.data.status === 'success') render(r.data);
    }).catch(function () {});
  }

  setInterval(function () {
    if (!mount()) return;
    var view = g('tradeView');
    var visible = !!view && !view.classList.contains('hidden') && sessionStorage.getItem('broker') === 'deriv';
    box.classList.toggle('hidden', !visible);
    if (!visible || document.visibilityState === 'hidden' || st.busy) return;
    var now = Date.now();
    if (now - st.t > (st.running ? 5000 : 15000)) { st.t = now; poll(); }
  }, 1500);
})();
