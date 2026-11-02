"""Phase 4: real-payout maths and a trade-journal report (additive).

Nothing here predicts the market. It measures what Deriv actually charges (true
break-even from ask/payout) and what your real results prove, so the bot and the
page stop relying on an assumed 5% margin.

Route (needs a logged-in Deriv session):
  POST /edge/report {"trades": [{"symbol","contract","win","stake","profit","be"}...]}
"""
import math
import os

Z95 = 1.645   # one-sided 95%


def breakeven_pct(ask, payout):
    """Win rate (%) needed to break even at the price Deriv really quoted."""
    try:
        ask, payout = float(ask), float(payout)
    except (TypeError, ValueError):
        return None
    if ask <= 0 or payout <= ask:
        return None
    return round(ask / payout * 100, 2)


def profit_pct(ask, payout):
    """Profit as % of stake if the contract wins."""
    try:
        ask, payout = float(ask), float(payout)
    except (TypeError, ValueError):
        return None
    if ask <= 0:
        return None
    return round((payout - ask) / ask * 100, 2)


def payout_ok(ask, payout, min_profit_pct):
    """Payout floor. Returns (ok, message). min_profit_pct <= 0 turns it off."""
    pp = profit_pct(ask, payout)
    if pp is None:
        return False, "Deriv quote has no usable payout. Not bought."
    if min_profit_pct > 0 and pp < min_profit_pct:
        return False, "Payout too low: a win pays %.0f%% of stake, floor is %.0f%%. Not bought." % (pp, min_profit_pct)
    return True, ""


def min_profit_floor():
    try:
        return max(0.0, min(float(os.getenv("MIN_PAYOUT_PCT", "80")), 200.0))
    except (TypeError, ValueError):
        return 80.0


def wilson_lower(k, n, z=Z95):
    """Lower bound of the true win rate (0..1). Honest about small samples."""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d)


def _stats(rows):
    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    pnl = [r["profit"] for r in rows]
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rows:
        eq += r["profit"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = 0 if r["win"] else streak + 1
        worst = max(worst, streak)
    bes = [r["be"] for r in rows if r.get("be")]
    be = round(sum(bes) / len(bes), 2) if bes else None
    out = {"n": n, "wins": wins, "win_pct": round(wins / n * 100, 1) if n else 0.0,
           "low_pct": round(wilson_lower(wins, n) * 100, 1), "be_pct": be,
           "profit": round(sum(pnl), 2), "per_trade": round(sum(pnl) / n, 3) if n else 0.0,
           "max_drawdown": round(dd, 2), "worst_streak": worst}
    if n < 100:
        out["verdict"] = "TOO_FEW"
        out["text"] = "%d of 100 trades: nothing can be concluded yet" % n
    elif be is None:
        out["verdict"] = "NO_BREAKEVEN"
        out["text"] = "Break-even unknown for these trades"
    elif out["low_pct"] > be:
        out["verdict"] = "EDGE"
        out["text"] = "Even the low estimate (%.1f%%) beats break-even (%.1f%%). Keep it on demo longer" % (out["low_pct"], be)
    elif out["win_pct"] < be - 2:
        out["verdict"] = "LOSING"
        out["text"] = "Win rate %.1f%% is under break-even %.1f%%. Stop or change the setup" % (out["win_pct"], be)
    else:
        out["verdict"] = "UNPROVEN"
        out["text"] = "No proven edge: win rate %.1f%% vs break-even %.1f%%" % (out["win_pct"], be)
    return out


def clean(trades):
    rows = []
    for t in trades if isinstance(trades, list) else []:
        try:
            rows.append({"symbol": str(t.get("symbol", ""))[:24], "contract": str(t.get("contract", ""))[:24],
                         "win": bool(t.get("win")), "profit": float(t.get("profit", 0)),
                         "be": float(t["be"]) if t.get("be") not in (None, "") else None})
        except (TypeError, ValueError, AttributeError):
            continue
    return rows[-2000:]


def report(trades):
    rows = clean(trades)
    groups = {}
    for r in rows:
        groups.setdefault((r["symbol"], r["contract"]), []).append(r)
    by = [dict(_stats(v), symbol=k[0], contract=k[1]) for k, v in groups.items()]
    by.sort(key=lambda x: x["per_trade"])
    return {"all": _stats(rows), "groups": by[:30]}


def register(core):
    from flask import jsonify, request

    @core.app.post("/edge/report")
    def edge_report():
        _, broker = core.current_session()
        if not broker:
            return core.denied_response()
        d = request.get_json(silent=True) or {}
        return jsonify(dict(report(d.get("trades")), status="success", floor=min_profit_floor()))
