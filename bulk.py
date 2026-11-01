"""SpaceX Bulk Trader backend: bulk orders, live-tick fallback, contract results.

Additive, like extras.py / derivs.py / auto.py. If this file fails to import, the
rest of the bot keeps working. Nothing here needs a paid key: it only uses your
free Deriv demo token and Deriv's public market-data socket.

Routes (all need a logged-in Deriv session):
  GET  /bulk/config    limits the page should respect
  GET  /bulk/ticks     recent ticks for one market (fallback when the browser
                       cannot open Deriv's public socket itself)
  POST /bulk/execute   buy 1..MAX_BULK identical contracts on one trading socket
  GET  /bulk/results   win/loss + profit for contract ids you bought
"""
import json
import time
import threading

from flask import jsonify, request

import derivs

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
HARD_MAX_BULK = 20

# UI contract -> derivs.CONTRACTS id.  side "a" = first button, "b" = second.
KINDS = {
    "even_odd": "even_odd",
    "rise_fall": "rise_fall",
    "over_under": "over_under",
    "match_differ": "match_differ",
}

_lock = threading.Lock()
_tick_cache = {}   # (symbol, count) -> (time, payload)


def _ws_close(ws):
    try:
        ws.close()
    except Exception:
        pass


def _decimals(values):
    """Most decimals seen in a price series (JSON drops trailing zeros)."""
    best = 0
    for v in values:
        s = repr(float(v))
        if "e" in s:
            continue
        if "." in s:
            best = max(best, len(s.split(".")[1].rstrip("0")))
    return best


def register(core):
    app = core.app
    BrokerError = core.BrokerError
    max_bulk = max(1, min(core.env_int("MAX_BULK", 5), HARD_MAX_BULK))

    def need():
        token, broker = core.current_session()
        if not broker:
            return None, None, core.denied_response()
        if broker.id != "deriv":
            return None, None, (
                jsonify({"status": "error", "message": "Bulk Trader works on the Deriv market."}),
                400,
            )
        return token, broker, None

    # ------------------------------------------------------------ config
    @app.get("/bulk/config")
    def bulk_config():
        _, broker, bad = need()
        if bad:
            return bad
        return jsonify({
            "status": "success",
            "max_stake": core.MAX_STAKE,
            "max_bulk": max_bulk,
            "allow_live": core.ALLOW_LIVE,
        })

    # ------------------------------------------------------------ ticks (fallback)
    @app.get("/bulk/ticks")
    def bulk_ticks():
        _, _, bad = need()
        if bad:
            return bad
        symbol = str(request.args.get("symbol", "")).strip()
        if not derivs.SYMBOL.match(symbol):
            return jsonify({"status": "error", "message": "Invalid market name."}), 400
        try:
            count = max(10, min(int(request.args.get("count", 120)), 500))
        except (TypeError, ValueError):
            count = 120
        key, now = (symbol, count), time.time()
        with _lock:
            hit = _tick_cache.get(key)
            if hit and now - hit[0] < 0.8:
                return jsonify(hit[1])
        try:
            out = _fetch_ticks(symbol, count)
        except BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        with _lock:
            _tick_cache[key] = (now, out)
            if len(_tick_cache) > 60:
                _tick_cache.clear()
        return jsonify(out)

    def _fetch_ticks(symbol, count):
        import websocket
        try:
            ws = websocket.create_connection(PUBLIC_WS, timeout=15)
        except Exception:
            raise BrokerError("Cannot connect to Deriv's price feed.", 503)
        try:
            ws.send(json.dumps({"ticks_history": symbol, "end": "latest", "count": count,
                                "style": "ticks", "req_id": 21}))
            for _ in range(10):
                msg = json.loads(ws.recv())
                if msg.get("req_id") not in (21, None):
                    continue
                if msg.get("error"):
                    err = msg["error"]
                    raise BrokerError("Deriv price data: " + str(err.get("message", err)), 502)
                hist = msg.get("history")
                if isinstance(hist, dict) and hist.get("prices"):
                    prices = [float(p) for p in hist["prices"]]
                    times = [int(t) for t in hist.get("times", [])]
                    pip = msg.get("pip_size")
                    try:
                        pip = int(pip) if pip is not None else _decimals(prices)
                    except (TypeError, ValueError):
                        pip = _decimals(prices)
                    return {"status": "success", "symbol": symbol, "prices": prices,
                            "times": times, "pip": pip}
            raise BrokerError("Deriv sent no tick data in time.", 504)
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("Deriv price feed dropped. Try again.", 503)
        finally:
            _ws_close(ws)

    # ------------------------------------------------------------ trading socket
    def _open_trading_ws(broker):
        import websocket
        acct = broker._pick()
        r, body = core.call("POST", f"{broker.API}/accounts/{acct.get('account_id')}/otp",
                            "Deriv", headers=broker._headers())
        if r.status_code >= 400 or not isinstance(body, dict):
            raise BrokerError(f"Deriv could not open a trading session ({r.status_code}).", 502)
        url = (body.get("data") or {}).get("url")
        if not url:
            raise BrokerError("Deriv did not return a trading session URL.", 502)
        try:
            return websocket.create_connection(url, timeout=20)
        except Exception:
            raise BrokerError("Cannot connect to Deriv's trading socket.", 503)

    def _spec(data):
        """Browser request -> derivs.build_proposal spec. Returns (spec, count, error)."""
        kind = KINDS.get(str(data.get("kind", "")))
        if not kind:
            return None, 0, "Unknown contract type."
        side = str(data.get("side", ""))
        if side not in ("a", "b"):
            return None, 0, "Pick the first or second button."
        try:
            count = int(data.get("count", 1))
            ticks = int(data.get("ticks", 1))
        except (TypeError, ValueError):
            return None, 0, "Ticks and number of trades must be numbers."
        if not 1 <= count <= max_bulk:
            return None, 0, f"Number of bulk trades must be 1 to {max_bulk} (MAX_BULK)."
        spec = {
            "contract": kind, "action": "up" if side == "a" else "down",
            "symbol": data.get("symbol"), "stake": data.get("stake"),
            "ticks": ticks, "duration": ticks, "unit": "t", "digit": data.get("digit"),
        }
        return spec, count, None

    # ------------------------------------------------------------ bulk execute
    @app.post("/bulk/execute")
    def bulk_execute():
        _, broker, bad = need()
        if bad:
            return bad
        data = request.get_json(silent=True) or {}
        spec, count, err = _spec(data)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        try:
            acct = broker.get_account()
            payload, err = derivs.build_proposal(spec, acct.get("currency") or "USD")
            if err:
                return jsonify({"status": "error", "message": err}), 400
            stake = payload["amount"]
            if stake * count > acct["balance"]:
                return jsonify({"status": "error",
                                "message": f"Not enough balance for {count} x {stake:.2f}."}), 403
            problem, _ = core.check_rules(broker, payload["underlying_symbol"], stake, "buy", acct)
            if problem:
                return jsonify({"status": "error", "message": problem}), 403
            placed, stopped = _place_many(broker, payload, count)
        except BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        if not placed:
            return jsonify({"status": "error", "message": stopped or "Nothing was bought."}), 502
        _remember(spec, payload, placed)
        return jsonify({"status": "success", "requested": count, "placed": placed,
                        "stopped": stopped})

    def _place_many(broker, payload, n):
        stake = payload["amount"]
        ws = _open_trading_ws(broker)
        placed, stopped = [], None
        try:
            for i in range(n):
                try:
                    prop = broker._ws_send(ws, payload, 100 + 2 * i).get("proposal") or {}
                    if not prop.get("id"):
                        raise BrokerError("Deriv returned no proposal. Check the market and settings.", 502)
                    ask = float(prop.get("ask_price", stake))
                    if ask > stake * 1.05 + 0.01:
                        raise BrokerError(f"Deriv price {ask} is above your stake {stake}. Not bought.", 403)
                    buy = broker._ws_send(ws, {"buy": prop["id"], "price": ask}, 101 + 2 * i).get("buy") or {}
                    placed.append({
                        "id": buy.get("contract_id", ""),
                        "price": buy.get("buy_price", ask),
                        "payout": buy.get("payout", prop.get("payout")),
                        "longcode": prop.get("longcode", ""),
                        "start": buy.get("start_time"),
                    })
                except BrokerError as e:
                    stopped = e.message
                    break
                except Exception:
                    stopped = "Deriv connection dropped part-way."
                    break
        finally:
            _ws_close(ws)
        return placed, stopped

    def _remember(spec, payload, placed):
        """Show bulk trades in the existing /history list."""
        try:
            import extras
            label = derivs.CONTRACTS[spec["contract"]]["actions"][spec["action"]]["label"]
            with extras._lock:
                for p in placed:
                    extras._trades.append({
                        "t": time.time(), "side": "buy" if spec["action"] == "up" else "sell",
                        "symbol": payload["underlying_symbol"], "size": payload["amount"],
                        "duration": payload.get("duration"), "order": p["id"], "contract": label})
                del extras._trades[:-200]
        except Exception:
            pass

    # ------------------------------------------------------------ results
    @app.get("/bulk/results")
    def bulk_results():
        _, broker, bad = need()
        if bad:
            return bad
        ids = []
        for raw in str(request.args.get("ids", "")).split(","):
            raw = raw.strip()
            if raw.isdigit():
                ids.append(int(raw))
        ids = ids[:40]
        if not ids:
            return jsonify({"status": "success", "results": {}})
        try:
            ws = _open_trading_ws(broker)
        except BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        out = {}
        try:
            for i, cid in enumerate(ids):
                try:
                    msg = broker._ws_send(ws, {"proposal_open_contract": 1, "contract_id": cid}, 300 + i)
                except BrokerError:
                    out[str(cid)] = {"state": "unknown"}
                    continue
                except Exception:
                    break
                out[str(cid)] = _read_contract(msg.get("proposal_open_contract") or {})
        finally:
            _ws_close(ws)
        return jsonify({"status": "success", "results": out})

    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _read_contract(c):
        profit = _num(c.get("profit"))
        status = str(c.get("status", "")).lower()
        done = bool(c.get("is_sold") or c.get("is_expired") or status in ("won", "lost", "sold"))
        if not done:
            return {"state": "open"}
        if status in ("won", "lost"):
            state = status
        else:
            state = "won" if (profit or 0) > 0 else "lost"
        exit_val = c.get("exit_tick_display_value") or c.get("exit_tick") or c.get("sell_spot")
        exit_s = str(exit_val) if exit_val is not None else ""
        return {
            "state": state,
            "profit": profit if profit is not None else 0.0,
            "payout": _num(c.get("payout")) if state == "won" else 0.0,
            "sell_price": _num(c.get("sell_price")),
            "entry": c.get("entry_tick_display_value") or c.get("entry_tick"),
            "exit": exit_val,
            "digit": int(exit_s[-1]) if exit_s[-1:].isdigit() else None,
        }
