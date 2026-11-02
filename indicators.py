"""Phase 2: indicator engine, market regime and confluence signal (additive).

Pure functions on candles [{"t","o","h","l","c"}, oldest first]. No network,
no Flask in the maths, so it is fully testable. It never invents data: with
too few or stale candles it answers WAIT with the reason.

A BUY or SELL needs ALL of: a clear trend, momentum in the same direction,
candles agreeing, a volatility level that is neither frozen nor spiking, and
no exhaustion (RSI) against it. Anything else is WAIT. This lowers how often
it trades; it cannot make price direction predictable.

Routes (need a logged-in Deriv session):
  POST /scan   {"symbols": [...up to 8], "granularity": 60}  -> one row per market
"""
import re
import time

MIN_CANDLES = 60
MIN_STRENGTH = 70           # strength needed for BUY/SELL


# ---------------------------------------------------------------- indicators
def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def ema_series(v, n):
    if len(v) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(v[:n]) / n]
    for x in v[n:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def ema(v, n):
    s = ema_series(v, n)
    return s[-1] if s else None


def rsi(v, n=14):
    """Wilder's RSI."""
    if len(v) <= n:
        return None
    g = l = 0.0
    for a, b in zip(v[:n], v[1:n + 1]):
        d = b - a
        g, l = (g + d, l) if d >= 0 else (g, l - d)
    g, l = g / n, l / n
    for a, b in zip(v[n:], v[n + 1:]):
        d = b - a
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return 100 - 100 / (1 + g / l)


def macd(v, fast=12, slow=26, sig=9):
    ef, es = ema_series(v, fast), ema_series(v, slow)
    if not es:
        return None
    ef = ef[len(ef) - len(es):]
    line = [a - b for a, b in zip(ef, es)]
    sg = ema_series(line, sig)
    if len(sg) < 2:
        return None
    hist = [a - b for a, b in zip(line[len(line) - len(sg):], sg)]
    return {"line": line[-1], "signal": sg[-1], "hist": hist[-1], "hist_prev": hist[-2]}


def atr_series(c, n=14):
    if len(c) < n + 1:
        return []
    tr = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
          for i in range(1, len(c))]
    out = [sum(tr[:n]) / n]
    for x in tr[n:]:
        out.append((out[-1] * (n - 1) + x) / n)
    return out


def efficiency(v, n=20):
    """Kaufman efficiency ratio: 1 = straight line, 0 = pure chop."""
    if len(v) <= n:
        return None
    path = sum(abs(b - a) for a, b in zip(v[-n - 1:-1], v[-n:]))
    return abs(v[-1] - v[-n - 1]) / path if path else 0.0


def candle_shape(c):
    rng = c["h"] - c["l"]
    if rng <= 0:
        return {"dir": 0, "body": 0.0, "up": 0.0, "low": 0.0}
    body = abs(c["c"] - c["o"])
    return {"dir": 1 if c["c"] > c["o"] else -1 if c["c"] < c["o"] else 0,
            "body": body / rng, "up": (c["h"] - max(c["o"], c["c"])) / rng,
            "low": (min(c["o"], c["c"]) - c["l"]) / rng}


# ---------------------------------------------------------------- analysis
def _wait(why, **extra):
    d = {"signal": "WAIT", "strength": 0, "reasons": [], "conflicts": [why], "regime": "unclear",
         "trend": "unclear", "action": "WAIT: " + why}
    d.update(extra)
    return d


def analyze(candles, gran=60, now=None):
    now = time.time() if now is None else now
    if not isinstance(candles, list) or len(candles) < MIN_CANDLES:
        return _wait("INSUFFICIENT DATA (need %d candles, have %d)" % (
            MIN_CANDLES, len(candles) if isinstance(candles, list) else 0))
    try:
        closes = [float(c["c"]) for c in candles]
        last_t = int(candles[-1]["t"])
    except (KeyError, TypeError, ValueError):
        return _wait("INSUFFICIENT DATA (bad candles)")
    age = now - last_t
    fresh = age <= gran * 3 + 30
    if not fresh:
        return _wait("STALE DATA (last candle %ds old)" % age, age=int(age), fresh=False)

    price = closes[-1]
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    s10, s30 = sma(closes, 10), sma(closes, 30)
    r = rsi(closes)
    m = macd(closes)
    atrs = atr_series(candles)
    er = efficiency(closes)
    if None in (e9, e21, e50, r, er) or m is None or len(atrs) < 20:
        return _wait("INSUFFICIENT DATA (indicators not ready)")

    atr_pct = atrs[-1] / price * 100 if price else 0.0
    base = sorted(atrs[-60:])[len(atrs[-60:]) // 2]
    vol_ratio = atrs[-1] / base if base else 1.0

    # --- regime
    if vol_ratio > 2.2:
        regime = "high_volatility"
    elif vol_ratio < 0.4:
        regime = "low_volatility"
    elif er < 0.25:
        regime = "sideways"
    elif e9 > e21 > e50 and price > e21:
        regime = "strong_uptrend" if er >= 0.4 else "weak_uptrend"
    elif e9 < e21 < e50 and price < e21:
        regime = "strong_downtrend" if er >= 0.4 else "weak_downtrend"
    else:
        regime = "unclear"
    trend = "up" if e9 > e21 > e50 else "down" if e9 < e21 < e50 else "mixed"

    shapes = [candle_shape(c) for c in candles[-3:]]
    dirs = [s["dir"] for s in shapes]
    last = shapes[-1]
    hist, hprev = m["hist"], m["hist_prev"]
    mom = "up" if hist > 0 and hist >= hprev else "down" if hist < 0 and hist <= hprev else \
        "fading_up" if hist > 0 else "fading_down" if hist < 0 else "flat"

    res = {"price": price, "ema9": e9, "ema21": e21, "ema50": e50, "sma10": s10, "sma30": s30,
           "rsi": round(r, 1), "macd": m["line"], "macd_hist": hist, "atr_pct": round(atr_pct, 4),
           "vol_ratio": round(vol_ratio, 2), "efficiency": round(er, 2), "regime": regime,
           "trend": trend, "momentum": mom, "age": int(age), "fresh": True}

    if regime in ("high_volatility", "low_volatility", "sideways", "unclear"):
        return dict(res, signal="WAIT", strength=0, reasons=[], conflicts=["regime is " + regime],
                    action="WAIT: market regime is " + regime.replace("_", " "))

    side = 1 if trend == "up" else -1 if trend == "down" else 0
    if side == 0:
        return dict(res, signal="WAIT", strength=0, reasons=[], conflicts=["EMAs not aligned"],
                    action="WAIT: no clear trend")

    # --- confluence scoring (each check is worth points, conflicts subtract)
    pts, why, bad = 0, [], []
    pts += 25 if er >= 0.4 else 12
    why.append("trend %s, efficiency %.2f" % (trend, er))
    if (side == 1 and mom == "up") or (side == -1 and mom == "down"):
        pts += 25
        why.append("MACD momentum agrees")
    elif mom.startswith("fading"):
        pts += 5
        bad.append("MACD momentum fading")
    else:
        bad.append("MACD momentum opposes")
    if side == 1 and 50 <= r <= 70 or side == -1 and 30 <= r <= 50:
        pts += 20
        why.append("RSI %.0f supports" % r)
    elif side == 1 and r > 70 or side == -1 and r < 30:
        bad.append("RSI %.0f stretched (late entry)" % r)
    else:
        bad.append("RSI %.0f on the wrong side" % r)
    agree = sum(1 for d in dirs if d == side)
    if agree >= 2 and dirs[-1] == side and last["body"] >= 0.4:
        pts += 15
        why.append("last candles agree")
    else:
        bad.append("candles do not confirm")
    wick = last["up"] if side == 1 else last["low"]
    if wick > 0.5:
        bad.append("long rejection wick against the move")
        pts -= 10
    if 0.6 <= vol_ratio <= 1.8:
        pts += 15
        why.append("volatility normal")
    else:
        bad.append("volatility not ideal (x%.1f)" % vol_ratio)

    strength = max(0, min(100, pts))
    hard = any(("opposes" in b) or ("wrong side" in b) or ("stretched" in b) or ("rejection" in b)
               or ("do not confirm" in b) for b in bad)
    if strength >= MIN_STRENGTH and not hard:
        sig = "BUY" if side == 1 else "SELL"
        act = "%s: %d/100, %s" % (sig, strength, "; ".join(why))
    else:
        sig, act = "WAIT", "WAIT: only %d/100 or conflicts (%s)" % (strength, "; ".join(bad) or "needs more confirmation")
    return dict(res, signal=sig, strength=strength, reasons=why, conflicts=bad, action=act)


# ---------------------------------------------------------------- routes
def register(core):
    from flask import jsonify, request
    app = core.app
    GRANS = (60, 120, 180, 300, 600, 900, 1800, 3600)

    @app.post("/scan")
    def scan_route():
        _, broker = core.current_session()
        if not broker:
            return core.denied_response()
        if broker.id != "deriv":
            return jsonify({"status": "error", "message": "Scanner works on the Deriv market."}), 400
        d = request.get_json(silent=True) or {}
        syms = d.get("symbols")
        if not isinstance(syms, list) or not 1 <= len(syms) <= 8:
            return jsonify({"status": "error", "message": "Send 1 to 8 market names."}), 400
        try:
            gran = int(d.get("granularity", 60))
        except (TypeError, ValueError):
            gran = 0
        if gran not in GRANS:
            return jsonify({"status": "error", "message": "Unsupported timeframe."}), 400
        fetch = (getattr(core, "extras_api", None) or {}).get("fetch_candles")
        if not fetch:
            return jsonify({"status": "error", "message": "Price feed is not loaded."}), 503
        rows = []
        for s in syms:
            s = str(s).strip()
            if not core.SYMBOL_RE.match(s):
                rows.append({"symbol": s, "signal": "WAIT", "action": "WAIT: invalid market name"})
                continue
            try:
                rows.append(dict(analyze(fetch(s, gran, 120), gran), symbol=s))
            except core.BrokerError as e:
                rows.append({"symbol": s, "signal": "WAIT", "strength": 0,
                             "action": "WAIT: INSUFFICIENT DATA (" + e.message + ")"})
        rows.sort(key=lambda r: r.get("strength", 0), reverse=True)
        return jsonify({"status": "success", "granularity": gran, "time": time.time(), "rows": rows})
