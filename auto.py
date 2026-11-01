"""Phase 1: automatic trading loop (Deriv RISE/FALL).

Additive, like extras.py. It calls the app's own /analyze and /execute routes,
so every safety rule, trade history entry and chart marker works exactly as
it does for the manual buttons.

Routes (need a logged-in Deriv session):
  GET  /auto/status   mode, settings, counters, recent log
  POST /auto/start    switch to AUTO (demo accounts only)
  POST /auto/stop     switch back to MANUAL
  GET  /auto.js       the front-end panel
"""
import threading
import time

from flask import jsonify, request, send_from_directory

GRANS = (60, 120, 180, 300, 600, 900, 1800, 3600)
DEFAULTS = {
    "symbol": "1HZ100V", "volume": 1.0, "duration": 5, "granularity": 60,
    "min_confidence": 65, "interval": 120, "max_per_hour": 6,
}


def register(core):
    app = core.app
    lock = threading.Lock()
    S = {
        "running": False, "token": "", "cfg": dict(DEFAULTS), "log": [],
        "trades": [], "open_until": 0.0, "errors": 0, "placed": 0,
        "waits": 0, "stop": None, "since": 0.0,
    }

    def note(msg):
        with lock:
            S["log"].append({"t": time.time(), "m": str(msg)[:300]})
            del S["log"][:-40]

    def halt(reason):
        with lock:
            was = S["running"]
            S["running"] = False
            ev = S["stop"]
        if ev:
            ev.set()
        if was:
            note("AUTO stopped: " + reason)

    def clean(data):
        """Validate settings from the browser. Returns (cfg, error)."""
        c = dict(S["cfg"])
        try:
            c["symbol"] = str(data.get("symbol", c["symbol"])).strip()
            c["volume"] = round(float(data.get("volume", c["volume"])), 2)
            c["duration"] = int(data.get("duration", c["duration"]))
            c["granularity"] = int(data.get("granularity", c["granularity"]))
            c["min_confidence"] = int(data.get("min_confidence", c["min_confidence"]))
            c["interval"] = int(data.get("interval", c["interval"]))
            c["max_per_hour"] = int(data.get("max_per_hour", c["max_per_hour"]))
        except (TypeError, ValueError):
            return None, "Some auto settings are not numbers."
        if not core.SYMBOL_RE.match(c["symbol"]):
            return None, "Invalid market name."
        if not c["volume"] > 0:
            return None, "Stake must be above 0."
        if not 1 <= c["duration"] <= 1440:
            return None, "Duration must be 1 to 1440 minutes."
        if c["granularity"] not in GRANS:
            return None, "Unsupported timeframe."
        c["min_confidence"] = max(50, min(95, c["min_confidence"]))
        c["interval"] = max(60, min(3600, c["interval"]))   # protects the free AI quota
        c["max_per_hour"] = max(1, min(30, c["max_per_hour"]))
        return c, None

    def cycle(client, hdr, cfg):
        now = time.time()
        if now < S["open_until"]:
            if not S["log"] or not S["log"][-1]["m"].startswith("Waiting"):
                note("Waiting for the open contract to finish before the next trade.")
            return
        with lock:
            S["trades"] = [t for t in S["trades"] if now - t < 3600]
            capped = len(S["trades"]) >= cfg["max_per_hour"]
        if capped:
            note(f"Hourly limit reached ({cfg['max_per_hour']} trades). Resting.")
            return

        r = client.post("/analyze", headers=hdr, json={
            "symbol": cfg["symbol"], "granularity": cfg["granularity"], "duration": cfg["duration"]})
        d = r.get_json(silent=True) or {}
        if r.status_code == 401:
            halt("login session expired. Log in and switch AUTO on again.")
            return
        if r.status_code != 200 or d.get("status") != "success":
            S["errors"] += 1
            note("Analysis failed: " + str(d.get("message", r.status_code)))
            if S["errors"] >= 3:
                halt("3 errors in a row.")
            return
        S["errors"] = 0
        sig, conf = d.get("signal", "WAIT"), int(d.get("confidence", 0))
        note(f"AI says {sig} {conf}%. {d.get('reason', '')}")
        if sig not in ("BUY", "SELL") or conf < cfg["min_confidence"]:
            S["waits"] += 1
            note(f"No trade (needs BUY/SELL at {cfg['min_confidence']}%+).")
            return

        side = "buy" if sig == "BUY" else "sell"
        r = client.post("/execute", headers=hdr, json={
            "side": side, "symbol": cfg["symbol"], "volume": cfg["volume"], "duration": cfg["duration"]})
        d = r.get_json(silent=True) or {}
        if r.status_code == 401:
            halt("login session expired. Log in and switch AUTO on again.")
        elif d.get("status") == "success":
            S["errors"] = 0
            S["placed"] += 1
            with lock:
                S["trades"].append(now)
            S["open_until"] = now + cfg["duration"] * 60 + 5
            note(f"{'RISE' if side == 'buy' else 'FALL'} placed on {cfg['symbol']}, stake {cfg['volume']}, order #{d.get('order')}.")
        else:
            msg = str(d.get("message", r.status_code))
            note("Order refused: " + msg)
            if r.status_code == 403 and any(k in msg for k in ("Daily", "Live account", "trading hours", "above MAX")):
                halt(msg)
            else:
                S["errors"] += 1
                if S["errors"] >= 3:
                    halt("3 errors in a row.")

    def loop(ev):
        client = app.test_client()
        hdr = {"Authorization": "Bearer " + S["token"], "Content-Type": "application/json"}
        while not ev.is_set():
            try:
                cycle(client, hdr, dict(S["cfg"]))
            except Exception as e:  # never let the thread die silently
                S["errors"] += 1
                note("Unexpected error: " + type(e).__name__)
                if S["errors"] >= 3:
                    halt("3 errors in a row.")
            ev.wait(S["cfg"]["interval"])

    def need_deriv():
        token, broker = core.current_session()
        if not broker:
            return None, None, core.denied_response()
        if broker.id != "deriv":
            return None, None, (jsonify({"status": "error", "message": "Auto mode works on the Deriv market for now."}), 400)
        return token, broker, None

    def snapshot():
        with lock:
            return {
                "status": "success", "running": S["running"], "mode": "auto" if S["running"] else "manual",
                "cfg": dict(S["cfg"]), "placed": S["placed"], "waits": S["waits"],
                "trades_last_hour": len([t for t in S["trades"] if time.time() - t < 3600]),
                "next_free": max(0, int(S["open_until"] - time.time())), "since": S["since"],
                "log": list(S["log"][-25:]),
            }

    @app.get("/auto/status")
    def auto_status():
        _, _, bad = need_deriv()
        return bad or jsonify(snapshot())

    @app.post("/auto/start")
    def auto_start():
        token, broker, bad = need_deriv()
        if bad:
            return bad
        cfg, err = clean(request.get_json(silent=True) or {})
        if err:
            return jsonify({"status": "error", "message": err}), 400
        try:
            acct = broker.get_account()
        except core.BrokerError as e:
            return jsonify({"status": "error", "message": e.message}), e.http
        if not acct["demo"]:
            return jsonify({"status": "error", "message": "AUTO mode is demo-only. Use a demo account."}), 403
        problem = broker.size_error(cfg["volume"])
        if problem:
            return jsonify({"status": "error", "message": problem}), 403
        with lock:
            if S["running"]:
                S["cfg"] = cfg
                return jsonify(snapshot())
            ev = threading.Event()
            S.update(cfg=cfg, token=token, running=True, stop=ev, errors=0,
                     open_until=0.0, since=time.time())
        note(f"AUTO started on {cfg['symbol']}: every {cfg['interval']}s, min {cfg['min_confidence']}% confidence, max {cfg['max_per_hour']} trades/hour.")
        threading.Thread(target=loop, args=(ev,), daemon=True).start()
        return jsonify(snapshot())

    @app.post("/auto/stop")
    def auto_stop():
        _, _, bad = need_deriv()
        if bad:
            return bad
        halt("switched to MANUAL.")
        return jsonify(snapshot())

    @app.get("/auto.js")
    def auto_js():
        resp = send_from_directory(core.PUBLIC_DIR, "auto.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
