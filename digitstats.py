"""Phase 3: server-side digit statistics (additive).

Pure maths on a tick-price series, same rules the page's Brain uses (z >= 1.64,
600 ticks minimum), so server and browser agree. It never invents an edge: on
synthetic markets digits are random, so most answers are "no bias".

Route (needs a logged-in Deriv session):
  POST /digits/stats {"symbols": [...up to 8], "count": 1000}
       -> per market: digit counts and %, even/odd and over/under z-scores,
          chi-square vs uniform, recent-window check, and a plain verdict.
"""
import json
import math
import re
import time

MIN_TICKS = 600
Z_NEED = 1.64            # one-sided ~95%, same as the page (BR_Z)
CHI2_CRIT_9 = 16.92      # chi-square, 9 degrees of freedom, 5% level
RECENT = 40              # a skew must still show in the last ticks


def last_digit(price, pip):
    """Last digit of a price at the market's decimal places."""
    return int(("%.*f" % (pip, float(price)))[-1])


def digits_from_prices(prices, pip):
    out = []
    for p in prices:
        try:
            out.append(last_digit(p, pip))
        except (TypeError, ValueError):
            continue
    return out


def zscore(k, n, p):
    if n <= 0 or not 0 < p < 1:
        return 0.0
    return (k - n * p) / math.sqrt(n * p * (1 - p))


def analyze(digits):
    n = len(digits)
    if n < MIN_TICKS:
        return {"ok": False, "n": n, "verdict": "LEARNING",
                "action": "WAIT: only %d of %d ticks, not enough to measure a bias" % (n, MIN_TICKS)}
    counts = [0] * 10
    for d in digits:
        counts[d] += 1
    exp = n / 10.0
    chi2 = sum((c - exp) ** 2 / exp for c in counts)
    pct = [round(c / n * 100, 2) for c in counts]

    ev = sum(counts[d] for d in (0, 2, 4, 6, 8))
    z_even = zscore(ev, n, 0.5)
    # over/under for each barrier 0..9: P(over b) = (9-b)/10, P(under b) = b/10
    over, under = {}, {}
    for b in range(10):
        if b < 9:
            over[b] = round(zscore(sum(counts[b + 1:]), n, (9 - b) / 10), 2)
        if b > 0:
            under[b] = round(zscore(sum(counts[:b]), n, b / 10), 2)

    rec = digits[-RECENT:]
    rc = [0] * 10
    for d in rec:
        rc[d] += 1
    rec_even = sum(rc[d] for d in (0, 2, 4, 6, 8))

    best = []   # strongest single-side leads
    if abs(z_even) >= Z_NEED:
        side = "even" if z_even > 0 else "odd"
        recent_ok = (rec_even / len(rec) > 0.5) == (z_even > 0)
        best.append({"kind": "even_odd", "side": side, "z": round(abs(z_even), 2),
                     "recent_ok": recent_ok})
    for b, z in over.items():
        if z >= Z_NEED:
            ro = sum(rc[b + 1:]) / len(rec) > (9 - b) / 10
            best.append({"kind": "over_under", "side": "over %d" % b, "z": z, "recent_ok": ro})
    for b, z in under.items():
        if z >= Z_NEED:
            ro = sum(rc[:b]) / len(rec) > b / 10
            best.append({"kind": "over_under", "side": "under %d" % b, "z": z, "recent_ok": ro})
    best.sort(key=lambda x: x["z"], reverse=True)
    usable = [b for b in best if b["recent_ok"]]

    uneven = chi2 >= CHI2_CRIT_9
    if usable:
        verdict, act = "BIAS", "Lead: %s (z %.1f) and still showing in the last %d ticks" % (
            usable[0]["side"], usable[0]["z"], RECENT)
    elif best:
        verdict, act = "FADING", "A lead exists (%s, z %.1f) but is not in the last %d ticks: wait" % (
            best[0]["side"], best[0]["z"], RECENT)
    else:
        verdict, act = "NONE", "No bias beyond chance over %d ticks" % n
    return {"ok": True, "n": n, "counts": counts, "pct": pct, "chi2": round(chi2, 2),
            "uneven": uneven, "z_even": round(z_even, 2), "over_z": over, "under_z": under,
            "leads": best[:5], "verdict": verdict, "action": act,
            "note": "Random digits show a 'bias' somewhere about once in several tests by chance; "
                    "a lead is not an edge."}


# ---------------------------------------------------------------- routes
def register(core):
    from flask import jsonify, request
    app = core.app
    PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"

    def fetch_ticks(symbol, count):
        import websocket
        try:
            ws = websocket.create_connection(PUBLIC_WS, timeout=15)
        except Exception:
            raise core.BrokerError("Cannot connect to Deriv's price feed.", 503)
        try:
            ws.send(json.dumps({"ticks_history": symbol, "end": "latest", "count": count,
                                "style": "ticks", "req_id": 31}))
            for _ in range(10):
                msg = json.loads(ws.recv())
                if msg.get("req_id") not in (31, None):
                    continue
                if msg.get("error"):
                    raise core.BrokerError("Deriv price data: " + str(msg["error"].get("message")), 502)
                h = msg.get("history")
                if isinstance(h, dict) and h.get("prices"):
                    prices = [float(p) for p in h["prices"]]
                    pip = msg.get("pip_size")
                    try:
                        pip = int(pip)
                    except (TypeError, ValueError):
                        pip = max((len(repr(p).split(".")[1].rstrip("0")) if "." in repr(p) else 0)
                                  for p in prices)
                    return prices, pip
            raise core.BrokerError("Deriv sent no tick data in time.", 504)
        except core.BrokerError:
            raise
        except Exception:
            raise core.BrokerError("Deriv price feed dropped. Try again.", 503)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @app.post("/digits/stats")
    def digits_stats():
        _, broker = core.current_session()
        if not broker:
            return core.denied_response()
        if broker.id != "deriv":
            return jsonify({"status": "error", "message": "Digit stats work on the Deriv market."}), 400
        d = request.get_json(silent=True) or {}
        syms = d.get("symbols")
        if not isinstance(syms, list) or not 1 <= len(syms) <= 8:
            return jsonify({"status": "error", "message": "Send 1 to 8 market names."}), 400
        try:
            count = max(MIN_TICKS, min(int(d.get("count", 1000)), 3000))
        except (TypeError, ValueError):
            count = 1000
        rows = []
        for s in syms:
            s = str(s).strip()
            if not core.SYMBOL_RE.match(s):
                rows.append({"symbol": s, "ok": False, "verdict": "INVALID", "action": "WAIT: invalid market name"})
                continue
            try:
                prices, pip = fetch_ticks(s, count)
                rows.append(dict(analyze(digits_from_prices(prices, pip)), symbol=s))
            except core.BrokerError as e:
                rows.append({"symbol": s, "ok": False, "verdict": "ERROR",
                             "action": "WAIT: INSUFFICIENT DATA (" + e.message + ")"})
        return jsonify({"status": "success", "time": time.time(), "rows": rows})
