"""Phase 2: every Deriv contract type, plus AI contract choice for AUTO mode.

Additive (like extras.py and auto.py). Everything goes through the same safety
checks as the manual buttons (real accounts only if ALLOW_LIVE, MAX_STAKE, daily
limits, trading hours).

Routes (need a logged-in Deriv session):
  GET  /derivs/catalog   contract types and their fields
  GET  /derivs/markets   markets that support a contract type
  POST /derivs/execute   place one contract
  POST /derivs/analyze   AI picks the best allowed contract (or WAIT)
  GET  /derivs.js        the front-end panel
"""
import json
import re
import threading
import time
from decimal import Decimal

from flask import jsonify, request, send_from_directory

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
SYMBOL = re.compile(r"^\w{2,30}$")


def A(label, typ, hint, bar=None):
    return {"label": label, "type": typ, "hint": hint, "bar": bar}


# bar: "+"/"-" = barrier is entry +/- offset; "raw" = signed offset typed by user; "digit" = last-digit pick
CONTRACTS = {
    "rise_fall": {"label": "Rise / Fall", "dur": "time", "fields": [], "actions": {
        "up": A("RISE", "CALL", "wins if the price ends higher than the entry price"),
        "down": A("FALL", "PUT", "wins if the price ends lower than the entry price")}},
    "higher_lower": {"label": "Higher / Lower", "dur": "time", "fields": ["barrier"], "actions": {
        "up": A("HIGHER", "HIGHER", "wins if the price ends above entry plus the barrier offset", "+"),
        "down": A("LOWER", "LOWER", "wins if the price ends below entry minus the barrier offset", "-")}},
    "touch": {"label": "Touch", "dur": "time", "fields": ["barrier"], "actions": {
        "up": A("TOUCH ABOVE", "ONETOUCH", "wins if the price touches entry plus offset before expiry", "+"),
        "down": A("TOUCH BELOW", "ONETOUCH", "wins if the price touches entry minus offset before expiry", "-")}},
    "no_touch": {"label": "No Touch", "dur": "time", "fields": ["barrier"], "actions": {
        "up": A("NO TOUCH BELOW", "NOTOUCH", "wins if the price never touches entry minus offset (bullish or flat view)", "-"),
        "down": A("NO TOUCH ABOVE", "NOTOUCH", "wins if the price never touches entry plus offset (bearish or flat view)", "+")}},
    "even_odd": {"label": "Even / Odd", "dur": "ticks", "fields": [], "actions": {
        "up": A("EVEN", "DIGITEVEN", "wins if the last digit of the final tick is even"),
        "down": A("ODD", "DIGITODD", "wins if the last digit of the final tick is odd")}},
    "over_under": {"label": "Over / Under", "dur": "ticks", "fields": ["digit"], "actions": {
        "up": A("OVER", "DIGITOVER", "wins if the last digit is above the chosen digit (0-8)", "digit"),
        "down": A("UNDER", "DIGITUNDER", "wins if the last digit is below the chosen digit (1-9)", "digit")}},
    "match_differ": {"label": "Matches / Differs", "dur": "ticks", "fields": ["digit"], "actions": {
        "up": A("MATCHES", "DIGITMATCH", "wins if the last digit equals the chosen digit", "digit"),
        "down": A("DIFFERS", "DIGITDIFF", "wins if the last digit differs from the chosen digit", "digit")}},
    "accumulator": {"label": "Accumulator", "dur": "none", "fields": ["growth_rate", "take_profit"], "actions": {
        "up": A("BUY ACCUMULATOR", "ACCU", "stake grows each tick the price stays inside a range; the whole stake is lost if it breaks out")}},
    "multiplier": {"label": "Multiplier", "dur": "none", "fields": ["multiplier", "stop_loss", "take_profit"], "actions": {
        "up": A("MULTIPLIER UP", "MULTUP", "profit and loss are multiplied while the price rises; closes at stop loss or take profit"),
        "down": A("MULTIPLIER DOWN", "MULTDOWN", "profit and loss are multiplied while the price falls; closes at stop loss or take profit")}},
    "vanilla": {"label": "Vanilla (experimental)", "dur": "time", "fields": ["barrier"], "experimental": True, "actions": {
        "up": A("VANILLA CALL", "VANILLALONGCALL", "pays if the price ends above the strike", "raw"),
        "down": A("VANILLA PUT", "VANILLALONGPUT", "pays if the price ends below the strike", "raw")}},
    "turbos": {"label": "Turbos (experimental)", "dur": "time", "fields": ["barrier"], "experimental": True, "actions": {
        "up": A("TURBOS LONG", "TURBOSLONG", "gains while the price stays above the barrier, knocked out if it falls to it", "raw"),
        "down": A("TURBOS SHORT", "TURBOSSHORT", "gains while the price stays below the barrier, knocked out if it rises to it", "raw")}},
}


def _num(x):
    return format(Decimal(str(round(float(x), 5))), "f").rstrip("0").rstrip(".") or "0"


def build_proposal(spec, currency="USD"):
    """Turn a UI/AUTO request into a Deriv proposal payload. Returns (payload, error)."""
    c = CONTRACTS.get(spec.get("contract"))
    if not c:
        return None, "Unknown contract type."
    act = c["actions"].get(spec.get("action"))
    if not act:
        return None, "That contract does not have that action."
    symbol = str(spec.get("symbol", "")).strip()
    if not SYMBOL.match(symbol):
        return None, "Invalid market name."
    try:
        stake = round(float(spec.get("stake")), 2)
        if not stake > 0:
            return None, "Stake must be above 0."
        p = {"proposal": 1, "amount": stake, "basis": "stake", "contract_type": act["type"],
             "currency": currency, "underlying_symbol": symbol}
        if c["dur"] == "time":
            unit = str(spec.get("unit", "m"))
            n = int(spec.get("duration", 5))
            if unit not in ("s", "m", "h", "t") or n < 1:
                return None, "Duration must be 1 or more, in s, m, h or t."
            p["duration"], p["duration_unit"] = n, unit
        elif c["dur"] == "ticks":
            n = int(spec.get("ticks", 5))
            if not 1 <= n <= 10:
                return None, "Ticks must be 1 to 10."
            p["duration"], p["duration_unit"] = n, "t"
        bar = act["bar"]
        if bar in ("+", "-"):
            b = abs(float(spec.get("barrier")))
            if not b > 0:
                return None, "Barrier offset must be above 0."
            p["barrier"] = bar + _num(b)
        elif bar == "raw":
            v = float(spec.get("barrier"))
            if v == 0:
                return None, "Enter a signed barrier offset such as +0.5 or -0.5."
            p["barrier"] = ("-" if v < 0 else "+") + _num(abs(v))
        elif bar == "digit":
            d = int(spec.get("digit"))
            if not 0 <= d <= 9 or (act["type"] == "DIGITOVER" and d > 8) or (act["type"] == "DIGITUNDER" and d < 1):
                return None, "Digit out of range (Over 0-8, Under 1-9, Matches/Differs 0-9)."
            p["barrier"] = str(d)
        if "growth_rate" in c["fields"]:
            g = float(spec.get("growth_rate"))
            tp = float(spec.get("take_profit"))
            if not 0.01 <= g <= 0.05:
                return None, "Growth rate must be between 0.01 and 0.05."
            if not tp > 0:
                return None, "Accumulators need a take profit amount."
            p["growth_rate"] = g
            p["limit_order"] = {"take_profit": round(tp, 2)}
        if "multiplier" in c["fields"]:
            m, sl, tp = float(spec.get("multiplier")), float(spec.get("stop_loss")), float(spec.get("take_profit"))
            if not m >= 1:
                return None, "Multiplier must be 1 or more."
            if not 0 < sl <= stake:
                return None, "Stop loss must be above 0 and not above your stake."
            if not tp > 0:
                return None, "Multipliers need a take profit amount."
            p["multiplier"] = int(m) if m == int(m) else m
            p["limit_order"] = {"stop_loss": round(sl, 2), "take_profit": round(tp, 2)}
    except (TypeError, ValueError):
        return None, "Some contract settings are missing or not numbers."
    return p, None


def hold_seconds(cid, params, cooldown=300):
    """How long AUTO waits after opening this contract before opening another."""
    kind = CONTRACTS[cid]["dur"]
    try:
        if kind == "time":
            unit, n = str(params.get("unit", "m")), int(params.get("duration", 5))
            return n * 2 + 10 if unit == "t" else n * UNIT_SECONDS.get(unit, 60) + 5
        if kind == "ticks":
            return int(params.get("ticks", 5)) * 2 + 10
    except (TypeError, ValueError):
        pass
    return cooldown   # multipliers/accumulators close on their own limits


def check_config(cfg):
    """Dry-run every allowed contract/action so AUTO fails at start, not mid-run."""
    for cid in cfg["contracts"]:
        for key in CONTRACTS[cid]["actions"]:
            spec = dict(cfg["params"], contract=cid, action=key, symbol=cfg["symbol"], stake=cfg["volume"])
            _, err = build_proposal(spec)
            if err:
                return f"{CONTRACTS[cid]['label']}: {err}"
    return None


_MK_CACHE = {}   # types -> (time, list); the market list rarely changes


def _ws_markets(core, types):
    import time as _t
    key = json.dumps(types, sort_keys=True, default=str)
    hit = _MK_CACHE.get(key)
    if hit and _t.time() - hit[0] < 300:
        return hit[1]
    rows = _ws_markets_live(core, types)
    _MK_CACHE[key] = (_t.time(), rows)
    return rows


def _ws_markets_live(core, types):
    import websocket
    try:
        ws = websocket.create_connection(PUBLIC_WS, timeout=15)
    except Exception:
        raise core.BrokerError("Cannot connect to Deriv's market list.", 503)
    try:
        ws.send(json.dumps({"active_symbols": "brief", "contract_type": types, "req_id": 11}))
        for _ in range(10):
            msg = json.loads(ws.recv())
            if msg.get("req_id") not in (11, None):
                continue
            if msg.get("error"):
                raise core.BrokerError("Deriv markets: " + str(msg["error"].get("message", msg["error"])), 502)
            if isinstance(msg.get("active_symbols"), list):
                return msg["active_symbols"]
        raise core.BrokerError("Deriv sent no market list in time.", 504)
    except core.BrokerError:
        raise
    except Exception:
        raise core.BrokerError("Deriv market list dropped. Try again.", 503)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _place(core, broker, payload):
    """proposal -> buy over Deriv's trading socket. Returns (contract_id, price, longcode)."""
    import websocket
    stake = payload["amount"]
    acct = broker._pick()
    r, body = core.call("POST", f"{broker.API}/accounts/{acct.get('account_id')}/otp", "Deriv",
                        headers=broker._headers())
    if r.status_code >= 400 or not isinstance(body, dict):
        raise core.BrokerError(f"Deriv could not open a trading session ({r.status_code}).", 502)
    url = (body.get("data") or {}).get("url")
    if not url:
        raise core.BrokerError("Deriv did not return a trading session URL.", 502)
    try:
        ws = websocket.create_connection(url, timeout=20)
    except Exception:
        raise core.BrokerError("Cannot connect to Deriv's trading socket.", 503)
    try:
        prop = broker._ws_send(ws, payload, 1).get("proposal") or {}
        if not prop.get("id"):
            raise core.BrokerError("Deriv returned no proposal. Check the market, contract and settings.", 502)
        ask = float(prop.get("ask_price", stake))
        if ask > stake * 1.05 + 0.01:
            raise core.BrokerError(f"Deriv price {ask} is above your stake {stake}. Not bought.", 403)
        buy = broker._ws_send(ws, {"buy": prop["id"], "price": ask}, 2).get("buy") or {}
    except core.BrokerError:
        raise
    except Exception:
        raise core.BrokerError("Deriv connection dropped. Try again.", 503)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return buy.get("contract_id", ""), buy.get("buy_price", ask), prop.get("longcode", "")


def _parse_pick(text, allowed):
    data = {}
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, TypeError):
        data = {}
    action = str(data.get("action", "WAIT")).strip()
    if action not in allowed:
        action = "WAIT"
    try:
        conf = max(0, min(100, int(float(data.get("confidence", 0)))))
    except (TypeError, ValueError):
        conf = 0
    digit = data.get("digit")
    try:
        digit = int(digit) if digit is not None else None
    except (TypeError, ValueError):
        digit = None
    return {"action": action, "confidence": conf, "digit": digit, "reason": str(data.get("reason", "")).strip()[:400]}


def register(core):
    app = core.app
    lock = threading.Lock()
    markets_cache = {}
    last = {}

    core.derivs_api = {"CONTRACTS": CONTRACTS, "check": check_config, "hold": hold_seconds}

    def need():
        token, broker = core.current_session()
        if not broker:
            return None, None, core.denied_response()
        if broker.id != "deriv":
            return None, None, (jsonify({"status": "error", "message": "Derivative contracts are for the Deriv market."}), 400)
        return token, broker, None

    @app.get("/derivs/catalog")
    def d_catalog():
        _, _, bad = need()
        if bad:
            return bad
        out = [{"id": k, "label": c["label"], "dur": c["dur"], "fields": c["fields"],
                "experimental": bool(c.get("experimental")),
                "actions": {a: {"label": v["label"], "hint": v["hint"]} for a, v in c["actions"].items()}}
               for k, c in CONTRACTS.items()]
        return jsonify({"status": "success", "contracts": out})

    @app.get("/derivs/markets")
    def d_markets():
        _, _, bad = need()
        if bad:
            return bad
        c = CONTRACTS.get(request.args.get("contract", "rise_fall"))
        if not c:
            return jsonify({"status": "error", "message": "Unknown contract type."}), 400
        types = sorted({a["type"] for a in c["actions"].values()})
        key, now = ",".join(types), time.time()
        with lock:
            hit = markets_cache.get(key)
        if hit and now - hit[0] < 300:
            return jsonify({"status": "success", "markets": hit[1]})
        try:
            rows = _ws_markets(core, types)
        except core.BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        out = sorted(
            ({"symbol": r.get("underlying_symbol"), "name": r.get("underlying_symbol_name") or r.get("underlying_symbol"),
              "market": r.get("market", ""), "submarket": r.get("submarket", "")}
             for r in rows if r.get("underlying_symbol") and not r.get("is_trading_suspended") and r.get("exchange_is_open", 1)),
            key=lambda x: (x["market"], x["submarket"], x["name"]))
        with lock:
            markets_cache[key] = (now, out)
        return jsonify({"status": "success", "markets": out})

    @app.post("/derivs/execute")
    def d_execute():
        _, broker, bad = need()
        if bad:
            return bad
        spec = request.get_json(silent=True) or {}
        try:
            acct = broker.get_account()
            payload, err = build_proposal(spec, acct.get("currency") or "USD")
            if err:
                return jsonify({"status": "error", "message": err}), 400
            problem, _ = core.check_rules(broker, payload["underlying_symbol"], payload["amount"], "buy", acct)
            if problem:
                return jsonify({"status": "error", "message": problem}), 403
            order, price, longcode = _place(core, broker, payload)
        except core.BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        try:   # show it in the existing trade history / chart markers
            import extras
            c = CONTRACTS[spec["contract"]]
            with extras._lock:
                extras._trades.append({"t": time.time(), "side": "buy" if spec["action"] == "up" else "sell",
                                       "symbol": payload["underlying_symbol"], "size": payload["amount"],
                                       "duration": payload.get("duration"), "order": order,
                                       "contract": c["actions"][spec["action"]]["label"]})
                del extras._trades[:-200]
        except Exception:
            pass
        return jsonify({"status": "success", "order": order, "price": price, "longcode": longcode})

    @app.post("/derivs/analyze")
    def d_analyze():
        token, _, bad = need()
        if bad:
            return bad
        now = time.time()
        with lock:
            if now - last.get(token, 0) < 6:
                return jsonify({"status": "error", "message": "Wait a few seconds between analyses."}), 429
            last[token] = now
            if len(last) > 200:
                last.clear()
        data = request.get_json(silent=True) or {}
        symbol = str(data.get("symbol", "")).strip()
        cons = [x for x in (data.get("contracts") or ["rise_fall"]) if x in CONTRACTS]
        try:
            gran = int(data.get("granularity", 60))
        except (TypeError, ValueError):
            gran = 60
        api = getattr(core, "extras_api", None)
        if not api or not core.SYMBOL_RE.match(symbol) or not cons:
            return jsonify({"status": "error", "message": "Analysis is not available (check market and contracts)."}), 400
        allowed = {}
        for cid in cons:
            for key, a in CONTRACTS[cid]["actions"].items():
                allowed[f"{cid}:{key}"] = f"{a['label']} ({CONTRACTS[cid]['label']}): {a['hint']}"
        try:
            candles = api["fetch_candles"](symbol, gran, 120)
            closes = [c["c"] for c in candles]
            recent = candles[-60:]
            rows = ";".join(f"{c['o']:g},{c['h']:g},{c['l']:g},{c['c']:g}" for c in recent)
            sma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
            sma30 = sum(closes[-30:]) / 30 if len(closes) >= 30 else None
            facts = [f"last close {closes[-1]:g}"]
            if sma10 is not None:
                facts.append(f"SMA10 {sma10:g}")
            if sma30 is not None:
                facts.append(f"SMA30 {sma30:g}")
            digits_note = ""
            if any(CONTRACTS[x]["dur"] == "ticks" for x in cons):
                dec = max(len(str(v).split(".")[1]) if "." in str(v) else 0 for v in closes[-60:])
                ds = [format(v, f".{dec}f")[-1] for v in closes[-60:]]
                freq = {str(d): ds.count(str(d)) for d in range(10)}
                digits_note = (f"Last digits of the last {len(ds)} closes, count per digit: {freq}. "
                               "Digits on synthetic markets are essentially random, so answer WAIT unless there is a clear skew. ")
            hold = f"{data.get('ticks', 5)} ticks" if all(CONTRACTS[x]["dur"] == "ticks" for x in cons) \
                else f"{data.get('duration', 5)}{data.get('unit', 'm')}"
            menu = "\n".join(f"- {k}: {v}" for k, v in allowed.items())
            prompt = (
                f"Market: {symbol}. Candle size: {gran} seconds. Planned holding time: {hold}.\n"
                f"Last {len(recent)} candles, oldest to newest, open,high,low,close separated by ';':\n{rows}\n"
                f"Indicators: {', '.join(facts)}. {digits_note}\n"
                f"You may only choose one of these actions, or WAIT:\n{menu}\n"
                'Reply with ONLY this JSON: {"action":"<one id above or WAIT>","confidence":0-100,'
                '"digit":<0-9 only for digit actions, else null>,"reason":"at most two short sentences"}. '
                "Keep confidence low unless the evidence is strong. Prefer WAIT when unclear."
            )
            pick = _parse_pick(api["ask_signal"](prompt), allowed)
        except core.BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        out = {"status": "success", "symbol": symbol, "time": time.time(), "confidence": pick["confidence"],
               "reason": pick["reason"], "digit": pick["digit"], "action": pick["action"], "signal": "WAIT"}
        if pick["action"] != "WAIT":
            cid, key = pick["action"].split(":")
            out.update(contract=cid, side=key, label=CONTRACTS[cid]["actions"][key]["label"],
                       signal="BUY" if key == "up" else "SELL")
        return jsonify(out)

    @app.get("/derivs.js")
    def d_js():
        resp = send_from_directory(core.PUBLIC_DIR, "derivs.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
