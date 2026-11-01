/* Chart panel for the dashboard. Additive: it adds a panel under the account box
   (Deriv only) and never changes the existing buttons or forms. */
(function () {
  'use strict';
  if (window.__vtChart) return;
  window.__vtChart = true;

  var TF = [[60, '1m'], [300, '5m'], [900, '15m'], [3600, '1h']];
  var st = {
    mode: 'price', gran: 60, key: '', candles: [], trades: [], balance: [],
    busyC: false, busyH: false, tC: 0, tH: 0, tA: 0, wasVisible: false, err: ''
  };
  var g = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------ styles + markup
  var css = document.createElement('style');
  css.textContent =
    '.container{max-width:720px !important}' +
    '.vt{text-align:left;margin:0 0 14px}' +
    '.vt button{width:auto;padding:6px 12px;font-size:13px;background:#3a3a3a;color:#fff;font-weight:normal}' +
    '.vt button:hover{background:#4a4a4a}' +
    '.vt button.on{background:#00e676;color:#121212;font-weight:bold}' +
    '.vt .bar{display:flex;gap:6px;align-items:center;margin-bottom:8px}' +
    '.vt select{width:auto;padding:6px;margin:0 0 0 auto;font-size:13px}' +
    '.vt canvas{width:100%;height:260px;display:block;background:#161616;border-radius:8px}' +
    '.vt .meta{font-size:12px;color:#aaa;margin:6px 0 10px;min-height:16px}' +
    '.vt .main{width:100%;padding:10px;font-size:14px;background:#00e676;color:#121212;font-weight:bold}' +
    '.vt .main:hover{background:#00c853}' +
    '.vt .main:disabled{background:#555;color:#ccc}' +
    '.vt .sig{margin-top:10px;font-size:13px;line-height:1.5;color:#ddd;word-break:break-word}' +
    '.vt .pill{display:inline-block;padding:2px 10px;border-radius:10px;font-weight:bold;margin-right:6px}' +
    '.vt .pill.BUY{background:#2e7d32;color:#fff}.vt .pill.SELL{background:#c62828;color:#fff}' +
    '.vt .pill.WAIT{background:#555;color:#fff}' +
    '.vt .note{font-size:11px;color:#888;margin-top:8px}' +
    '.vt table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}' +
    '.vt td,.vt th{padding:4px 6px;border-bottom:1px solid #2c2c2c;color:#ccc;text-align:left;font-weight:normal}' +
    '.vt th{color:#888}.vt .up{color:#00e676}.vt .dn{color:#ff5252}';
  document.head.appendChild(css);

  var panel = document.createElement('div');
  panel.className = 'vt hidden';
  panel.innerHTML =
    '<div class="bar">' +
    '<button type="button" data-m="price" class="on">Price</button>' +
    '<button type="button" data-m="balance">Balance</button>' +
    '<select id="vtTf" aria-label="Timeframe"></select></div>' +
    '<canvas id="vtCanvas"></canvas>' +
    '<div class="meta" id="vtMeta"></div>' +
    '<button type="button" class="main" id="vtAnalyze">Analyze with AI</button>' +
    '<div class="sig" id="vtSig"></div>' +
    '<div class="note">A suggestion only. Short-term moves on these markets are close to random, so no analysis ' +
    'makes trades reliably profitable. Keep to demo and small stakes.</div>' +
    '<div id="vtTrades"></div>';

  var tf = panel.querySelector('#vtTf');
  TF.forEach(function (x) {
    var o = document.createElement('option');
    o.value = x[0]; o.textContent = x[1]; tf.appendChild(o);
  });

  var mounted = false;
  function mount() {
    if (mounted) return true;
    var acct = g('acct');
    if (!acct || !acct.parentNode) return false;
    acct.parentNode.insertBefore(panel, acct.nextSibling);
    mounted = true;

    panel.querySelectorAll('button[data-m]').forEach(function (b) {
      b.addEventListener('click', function () {
        st.mode = b.getAttribute('data-m');
        panel.querySelectorAll('button[data-m]').forEach(function (x) {
          x.classList.toggle('on', x === b);
        });
        draw();
        if (st.mode === 'balance') loadHistory(true);
      });
    });
    tf.addEventListener('change', function () {
      st.gran = parseInt(tf.value, 10) || 60;
      st.candles = []; st.key = ''; draw(); loadCandles(true);
    });
    var sym = g('symbol');
    if (sym) sym.addEventListener('change', function () { st.candles = []; st.key = ''; draw(); loadCandles(true); });
    g('vtAnalyze').addEventListener('click', analyze);
    window.addEventListener('resize', draw);
    return true;
  }

  // ------------------------------------------------------------ data
  function call(path, opt) {
    opt = opt || {};
    opt.headers = Object.assign({
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + (sessionStorage.getItem('token') || '')
    }, opt.headers || {});
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return { ok: r.ok, status: r.status, data: d };
      });
    });
  }

  function symbol() { var s = g('symbol'); return s ? s.value.trim() : ''; }

  function loadCandles(force) {
    var sym = symbol();
    if (!sym || st.busyC) return;
    var now = Date.now();
    if (!force && now - st.tC < 8000) return;
    st.busyC = true; st.tC = now;
    var key = sym + '|' + st.gran;
    call('/candles?symbol=' + encodeURIComponent(sym) + '&granularity=' + st.gran + '&count=120')
      .then(function (r) {
        if (r.ok && r.data.status === 'success') {
          st.candles = r.data.candles || []; st.key = key; st.err = '';
        } else {
          st.err = (r.data && r.data.message) || 'Could not load prices';
        }
      })
      .catch(function () { st.err = 'Cannot reach the server.'; })
      .then(function () { st.busyC = false; draw(); renderMeta(); });
  }

  function loadHistory(force) {
    if (st.busyH) return;
    var now = Date.now();
    if (!force && now - st.tH < 15000) return;
    st.busyH = true; st.tH = now;
    call('/history')
      .then(function (r) {
        if (r.ok && r.data.status === 'success') {
          st.trades = r.data.trades || []; st.balance = r.data.balance || [];
        }
      })
      .catch(function () {})
      .then(function () { st.busyH = false; draw(); renderMeta(); renderTrades(); });
  }

  // ------------------------------------------------------------ drawing
  function fmt(v) {
    var a = Math.abs(v);
    return v.toFixed(a >= 100 ? 2 : a >= 1 ? 4 : 5);
  }
  function hhmm(sec) {
    var d = new Date(sec * 1000);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function setup() {
    var cv = g('vtCanvas');
    if (!cv) return null;
    var w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return null;
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = '11px sans-serif';
    return { ctx: ctx, w: w, h: h };
  }

  function note(c, text) {
    c.ctx.fillStyle = '#888'; c.ctx.textAlign = 'center';
    c.ctx.fillText(text, c.w / 2, c.h / 2);
  }

  function tri(ctx, x, y, up, color) {
    ctx.fillStyle = color; ctx.beginPath();
    if (up) { ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y + 3); ctx.lineTo(x + 5, y + 3); }
    else { ctx.moveTo(x, y + 6); ctx.lineTo(x - 5, y - 3); ctx.lineTo(x + 5, y - 3); }
    ctx.closePath(); ctx.fill();
  }

  function drawPrice(c) {
    var k = st.candles, ctx = c.ctx;
    if (!k.length) { note(c, st.err || 'Loading prices...'); return; }
    var L = 8, R = 62, T = 10, B = 18, pw = c.w - L - R, ph = c.h - T - B;
    var lo = Infinity, hi = -Infinity;
    k.forEach(function (x) { if (x.l < lo) lo = x.l; if (x.h > hi) hi = x.h; });
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    var X = function (i) { return L + (i + 0.5) * pw / k.length; };
    var Y = function (v) { return T + (hi - v) * ph / (hi - lo); };

    ctx.strokeStyle = '#262626'; ctx.fillStyle = '#777'; ctx.textAlign = 'left'; ctx.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
      ctx.fillText(fmt(v), L + pw + 4, y + 4);
    }
    var bw = Math.max(1, pw / k.length * 0.7);
    k.forEach(function (x, i) {
      var up = x.c >= x.o, col = up ? '#00e676' : '#ff5252', cx = X(i);
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.beginPath(); ctx.moveTo(cx, Y(x.h)); ctx.lineTo(cx, Y(x.l)); ctx.stroke();
      var top = Y(Math.max(x.o, x.c)), hgt = Math.max(1, Math.abs(Y(x.o) - Y(x.c)));
      ctx.fillRect(cx - bw / 2, top, bw, hgt);
    });
    var last = k[k.length - 1].c, ly = Y(last);
    ctx.strokeStyle = '#ffeb3b'; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(L, ly); ctx.lineTo(L + pw, ly); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#ffeb3b'; ctx.fillText(fmt(last), L + pw + 4, ly + 4);

    ctx.fillStyle = '#777'; ctx.textAlign = 'center';
    [0, Math.floor(k.length / 2), k.length - 1].forEach(function (i) {
      ctx.fillText(hhmm(k[i].t), Math.min(Math.max(X(i), L + 14), L + pw - 14), c.h - 4);
    });

    var sym = symbol(), t0 = k[0].t, t1 = k[k.length - 1].t + st.gran;
    st.trades.forEach(function (tr) {
      if (tr.symbol !== sym || tr.t < t0 || tr.t > t1) return;
      var idx = Math.min(k.length - 1, Math.max(0, Math.floor((tr.t - t0) / st.gran)));
      if (tr.side === 'buy') tri(ctx, X(idx), Y(k[idx].l) + 10, true, '#00e676');
      else tri(ctx, X(idx), Y(k[idx].h) - 10, false, '#ff5252');
    });
  }

  function drawBalance(c) {
    var b = st.balance, ctx = c.ctx;
    if (b.length < 2) {
      note(c, 'Balance history builds up as you use the bot.');
      return;
    }
    var L = 8, R = 62, T = 10, B = 18, pw = c.w - L - R, ph = c.h - T - B;
    var t0 = b[0][0], t1 = b[b.length - 1][0], lo = Infinity, hi = -Infinity;
    b.forEach(function (p) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; });
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
    var span = Math.max(1, t1 - t0);
    var X = function (t) { return L + (t - t0) * pw / span; };
    var Y = function (v) { return T + (hi - v) * ph / (hi - lo); };

    ctx.strokeStyle = '#262626'; ctx.fillStyle = '#777'; ctx.textAlign = 'left'; ctx.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
      ctx.fillText(v.toFixed(2), L + pw + 4, y + 4);
    }
    var up = b[b.length - 1][1] >= b[0][1], col = up ? '#00e676' : '#ff5252';
    ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath();
    b.forEach(function (p, i) { if (i) ctx.lineTo(X(p[0]), Y(p[1])); else ctx.moveTo(X(p[0]), Y(p[1])); });
    ctx.stroke(); ctx.lineWidth = 1;

    st.trades.forEach(function (tr) {
      if (tr.t < t0 || tr.t > t1) return;
      var x = X(tr.t), buy = tr.side === 'buy';
      ctx.strokeStyle = buy ? '#2e7d32' : '#c62828';
      ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
      ctx.setLineDash([]); tri(ctx, x, T + 6, buy, buy ? '#00e676' : '#ff5252');
    });
    ctx.fillStyle = '#777'; ctx.textAlign = 'center';
    ctx.fillText(hhmm(t0), L + 14, c.h - 4);
    ctx.fillText(hhmm(t1), L + pw - 14, c.h - 4);
  }

  function draw() {
    var c = setup();
    if (!c) return;
    if (st.mode === 'balance') drawBalance(c); else drawPrice(c);
  }

  // ------------------------------------------------------------ text parts
  function renderMeta() {
    var m = g('vtMeta');
    if (!m) return;
    if (st.mode === 'balance') {
      var b = st.balance;
      m.textContent = b.length > 1
        ? 'Balance ' + b[b.length - 1][1].toFixed(2) + ' (' +
          (b[b.length - 1][1] - b[0][1] >= 0 ? '+' : '') + (b[b.length - 1][1] - b[0][1]).toFixed(2) +
          ' since the server started tracking). Resets when the server restarts.'
        : 'Resets when the server restarts.';
      return;
    }
    var k = st.candles;
    if (!k.length) { m.textContent = st.err; return; }
    var chg = (k[k.length - 1].c / k[0].c - 1) * 100;
    m.textContent = symbol() + '  last ' + fmt(k[k.length - 1].c) + '  (' +
      (chg >= 0 ? '+' : '') + chg.toFixed(3) + '% over this view)' + (st.err ? '  ' + st.err : '');
  }

  function renderTrades() {
    var box = g('vtTrades');
    if (!box) return;
    box.textContent = '';
    if (!st.trades.length) return;
    var tbl = document.createElement('table');
    var head = tbl.insertRow();
    ['Time', 'Side', 'Market', 'Stake', 'Mins'].forEach(function (h) {
      var th = document.createElement('th'); th.textContent = h; head.appendChild(th);
    });
    st.trades.slice(-8).reverse().forEach(function (tr) {
      var row = tbl.insertRow(), buy = tr.side === 'buy';
      var cells = [hhmm(tr.t), buy ? 'RISE' : 'FALL', tr.symbol, tr.size, tr.duration];
      cells.forEach(function (v, i) {
        var td = row.insertCell(); td.textContent = v == null ? '' : v;
        if (i === 1) td.className = buy ? 'up' : 'dn';
      });
    });
    box.appendChild(tbl);
  }

  // ------------------------------------------------------------ AI signal
  function analyze() {
    var btn = g('vtAnalyze'), out = g('vtSig');
    var sym = symbol();
    if (!sym) { out.textContent = 'Enter a market first.'; return; }
    btn.disabled = true; out.textContent = 'Analyzing...';
    var dur = parseInt((g('duration') || {}).value, 10) || 5;
    call('/analyze', {
      method: 'POST',
      body: JSON.stringify({ symbol: sym, granularity: st.gran, duration: dur })
    }).then(function (r) {
      out.textContent = '';
      if (!r.ok || r.data.status !== 'success') {
        out.textContent = (r.data && r.data.message) || 'Analysis failed.';
        return;
      }
      var d = r.data, pill = document.createElement('span');
      pill.className = 'pill ' + d.signal;
      pill.textContent = d.signal === 'BUY' ? 'RISE (BUY)' : d.signal === 'SELL' ? 'FALL (SELL)' : 'WAIT';
      out.appendChild(pill);
      out.appendChild(document.createTextNode(d.confidence + '% confidence. ' + (d.reason || '')));
    }).catch(function () {
      out.textContent = 'Cannot reach the server.';
    }).then(function () { btn.disabled = false; });
  }

  // ------------------------------------------------------------ loop
  function tick() {
    if (!mount()) return;
    var view = g('tradeView');
    var visible = !!view && !view.classList.contains('hidden') &&
      sessionStorage.getItem('broker') === 'deriv';
    panel.classList.toggle('hidden', !visible);
    if (!visible) { st.wasVisible = false; return; }
    if (document.visibilityState === 'hidden') return;
    var now = Date.now();
    if (!st.wasVisible) {
      st.wasVisible = true; st.tA = now;
      draw(); loadCandles(true); loadHistory(true);
      return;
    }
    if (st.mode === 'price') loadCandles(false);
    loadHistory(false);
    if (now - st.tA > 30000) {            // keeps the balance curve moving
      st.tA = now;
      if (typeof window.refreshAccount === 'function') {
        try { window.refreshAccount().catch(function () {}); } catch (e) {}
      }
    }
    var cur = symbol() + '|' + st.gran;
    if (cur !== st.key && !st.busyC) loadCandles(true);
  }

  // after a trade, refresh the history straight away
  if (typeof window.sendSignal === 'function') {
    var orig = window.sendSignal;
    window.sendSignal = function () {
      var res = orig.apply(this, arguments);
      Promise.resolve(res).then(function () { loadHistory(true); }, function () {});
      return res;
    };
  }

  setInterval(tick, 1500);
  tick();
})();
