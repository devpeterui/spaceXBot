"""Extras for the dashboard: price candles, trade/balance history, AI signal.

Purely additive. app.py loads this at the end; if anything here fails to import,
the bot keeps working exactly as before.

Routes added (all need a logged-in session):
  GET  /candles   OHLC candles from Deriv's public market-data socket
  GET  /history   trades placed through /execute + balance snapshots (memory only)
  POST /analyze   asks the AI for BUY / SELL / WAIT from recent candles
  GET  /chart.js  the front-end chart script
"""
import json
import re
import threading
import time

import requests
from flask import jsonify, request, send_from_directory

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
GRANULARITIES = (60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400)

_lock = threading.Lock()
_trades = []        # newest last, capped
_balance = []       # [epoch, balance], capped
_candle_cache = {}  # (symbol, gran, count) -> (time, candles)
_last_analyze = {}  # session token -> time


# ---------------------------------------------------------------- helpers
def _note_balance(value):
    now = time.time()
    with _lock:
        if _balance:
            last_t, last_v = _balance[-1]
            if now - last_t < 15:
                return
            if value == last_v and now - last_t < 300:
                return
        _balance.append([now, value])
        del _balance[:-600]


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(vals, n=14):
    if len(vals) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _parse_signal(text):
    """Pull {signal, confidence, reason} out of whatever the model returned."""
    data = {}
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, TypeError):
        data = {}
    sig = str(data.get("signal", "WAIT")).strip().upper()
    if sig not in ("BUY", "SELL", "WAIT"):
        sig = "WAIT"
    try:
        conf = max(0, min(100, int(float(data.get("confidence", 0)))))
    except (TypeError, ValueError):
        conf = 0
    reason = str(data.get("reason", "")).strip()[:400]
    if not reason and not data:
        reason = "The AI reply could not be read, so treat this as WAIT."
    return {"signal": sig, "confidence": conf, "reason": reason}


# ---------------------------------------------------------------- register
def register(core):
    app = core.app
    BrokerError = core.BrokerError

    # ---- record trades and balances without touching the existing routes
    @app.after_request
    def _record(resp):
        try:
            if resp.status_code == 200 and resp.mimetype == "application/json":
                if request.path == "/execute" and request.method == "POST":
                    out = resp.get_json(silent=True) or {}
                    if out.get("status") == "success":
                        d = request.get_json(silent=True) or {}
                        with _lock:
                            _trades.append(
                                {
                                    "t": time.time(),
                                    "side": str(d.get("side", "")).lower(),
                                    "symbol": str(d.get("symbol", "")),
                                    "size": d.get("volume"),
                                    "duration": d.get("duration"),
                                    "order": out.get("order"),
                                }
                            )
                            del _trades[:-200]
                elif (request.path == "/account" and request.method == "GET") or (
                    request.path == "/login" and request.method == "POST"
                ):
                    out = resp.get_json(silent=True) or {}
                    acct = out.get("account") or {}
                    if "balance" in acct:
                        _note_balance(float(acct["balance"]))
        except Exception:
            pass
        return resp

    # ---- market data
    def fetch_candles(symbol, gran, count):
        key = (symbol, gran, count)
        now = time.time()
        with _lock:
            hit = _candle_cache.get(key)
            if hit and now - hit[0] < 4:
                return hit[1]
        try:
            import websocket  # websocket-client, already in requirements.txt

            ws = websocket.create_connection(PUBLIC_WS, timeout=15)
        except Exception:
            raise BrokerError("Cannot connect to Deriv's price feed.", 503)
        try:
            ws.send(
                json.dumps(
                    {
                        "ticks_history": symbol,
                        "end": "latest",
                        "count": count,
                        "style": "candles",
                        "granularity": gran,
                        "req_id": 7,
                    }
                )
            )
            raw = None
            for _ in range(10):
                msg = json.loads(ws.recv())
                if msg.get("req_id") not in (7, None):
                    continue
                if msg.get("error"):
                    err = msg["error"]
                    raise BrokerError("Deriv price data: " + str(err.get("message", err)), 502)
                if isinstance(msg.get("candles"), list):
                    raw = msg["candles"]
                    break
            if raw is None:
                raise BrokerError("Deriv sent no price data in time.", 504)
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("Deriv price feed dropped. Try again.", 503)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        candles = []
        for c in raw:
            try:
                candles.append(
                    {
                        "t": int(c["epoch"]),
                        "o": float(c["open"]),
                        "h": float(c["high"]),
                        "l": float(c["low"]),
                        "c": float(c["close"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not candles:
            raise BrokerError("No candles for that market. Check the symbol name.", 404)
        with _lock:
            _candle_cache[key] = (now, candles)
            if len(_candle_cache) > 40:
                _candle_cache.clear()
        return candles

    def need_deriv():
        token, broker = core.current_session()
        if not broker:
            return None, None, core.denied_response()
        if broker.id != "deriv":
            return None, None, (
                jsonify({"status": "error", "message": "Charts are available for the Deriv market."}),
                400,
            )
        return token, broker, None

    def read_market():
        symbol = str(request.values.get("symbol", "")).strip()
        if not core.SYMBOL_RE.match(symbol):
            raise BrokerError("Invalid symbol", 400)
        try:
            gran = int(request.values.get("granularity", 60))
            count = int(request.values.get("count", 120))
        except (TypeError, ValueError):
            raise BrokerError("Bad chart settings", 400)
        if gran not in GRANULARITIES:
            raise BrokerError("Unsupported timeframe", 400)
        return symbol, gran, max(10, min(count, 300))

    @app.get("/candles")
    def candles_route():
        _, _, bad = need_deriv()
        if bad:
            return bad
        try:
            symbol, gran, count = read_market()
            return jsonify({"status": "success", "symbol": symbol, "granularity": gran,
                            "candles": fetch_candles(symbol, gran, count)})
        except BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http

    @app.get("/history")
    def history_route():
        _, broker = core.current_session()
        if not broker:
            return core.denied_response()
        with _lock:
            return jsonify({"status": "success", "trades": list(_trades[-100:]),
                            "balance": list(_balance)})

    # ---- AI signal
    def ask_signal(prompt):
        if not core.AI_API_KEY:
            raise BrokerError("AI is not set up. Add AI_API_KEY in Render > Environment.", 503)
        system = (
            "You read price data for a demo trading dashboard. You cannot place trades. "
            "Short-term price moves are largely random, so be honest about uncertainty, "
            "answer WAIT when the picture is unclear, and never promise profit."
        )
        try:
            if core.AI_PROVIDER == "gemini":
                model = core.AI_MODEL or core.GEMINI_DEFAULT_MODEL
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                body = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
                }
                headers = {"x-goog-api-key": core.AI_API_KEY}
                r = requests.post(url, headers=headers, json=body, timeout=30)
                if r.status_code == 400:  # some models reject the JSON mime type: retry plain
                    body.pop("generationConfig")
                    r = requests.post(url, headers=headers, json=body, timeout=30)
                if r.status_code == 429:
                    raise BrokerError("AI rate limit reached. Wait a minute and try again.", 429)
                if r.status_code >= 400:
                    raise BrokerError(
                        f"AI provider error {r.status_code}. Check AI_API_KEY, AI_MODEL and your quota.", 502
                    )
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if core.AI_PROVIDER in core.OPENAI_COMPAT:
                url, default_model = core.OPENAI_COMPAT[core.AI_PROVIDER]
                r = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {core.AI_API_KEY}"},
                    json={
                        "model": core.AI_MODEL or default_model,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=30,
                )
                if r.status_code == 429:
                    raise BrokerError("AI rate limit reached. Wait a minute and try again.", 429)
                if r.status_code >= 400:
                    raise BrokerError(
                        f"AI provider error {r.status_code}. Check AI_API_KEY, AI_MODEL and your quota.", 502
                    )
                return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException:
            raise BrokerError("Cannot reach the AI provider.", 503)
        except (KeyError, IndexError, ValueError):
            raise BrokerError("AI provider sent an unexpected reply.", 502)
        raise BrokerError("AI_PROVIDER must be gemini, groq or xai.", 503)

    @app.post("/analyze")
    def analyze_route():
        token, _, bad = need_deriv()
        if bad:
            return bad
        now = time.time()
        with _lock:
            if now - _last_analyze.get(token, 0) < 6:
                return jsonify({"status": "error", "message": "Wait a few seconds between analyses."}), 429
            _last_analyze[token] = now
            if len(_last_analyze) > 200:
                _last_analyze.clear()
        data = request.get_json(silent=True) or {}
        symbol = str(data.get("symbol", "")).strip()
        try:
            gran = int(data.get("granularity", 60))
            minutes = max(1, min(int(data.get("duration", 5)), 1440))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Bad analysis settings"}), 400
        if not core.SYMBOL_RE.match(symbol) or gran not in GRANULARITIES:
            return jsonify({"status": "error", "message": "Invalid symbol or timeframe"}), 400
        try:
            candles = fetch_candles(symbol, gran, 120)
            closes = [c["c"] for c in candles]
            recent = candles[-60:]
            rows = ";".join(f"{c['o']:g},{c['h']:g},{c['l']:g},{c['c']:g}" for c in recent)
            sma10, sma30, rsi = _sma(closes, 10), _sma(closes, 30), _rsi(closes)
            change = (closes[-1] / closes[-11] - 1) * 100 if len(closes) > 10 and closes[-11] else 0.0
            facts = [f"last close {closes[-1]:g}", f"change over last 10 candles {change:+.3f}%"]
            if sma10 is not None:
                facts.append(f"SMA10 {sma10:g}")
            if sma30 is not None:
                facts.append(f"SMA30 {sma30:g}")
            if rsi is not None:
                facts.append(f"RSI14 {rsi:.1f}")
            prompt = (
                f"Market: {symbol}. Candle size: {gran} seconds. I plan to hold a RISE/FALL "
                f"contract for {minutes} minute(s).\n"
                f"Last {len(recent)} candles, oldest to newest, as open,high,low,close separated by ';':\n{rows}\n"
                f"Indicators: {', '.join(facts)}.\n"
                f"Give a directional view for the next {minutes} minute(s). Reply with ONLY this JSON: "
                '{"signal":"BUY" or "SELL" or "WAIT","confidence":0-100,"reason":"at most two short sentences"}. '
                "BUY means price rises, SELL means price falls. Keep confidence low unless the evidence is strong."
            )
            result = _parse_signal(ask_signal(prompt))
        except BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        result.update({"status": "success", "symbol": symbol, "time": time.time()})
        return jsonify(result)

    core.extras_api = {"fetch_candles": fetch_candles, "ask_signal": ask_signal}

    # ---- front-end script
    @app.get("/chart.js")
    def chart_js():
        resp = send_from_directory(core.PUBLIC_DIR, "chart.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
