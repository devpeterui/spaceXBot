"""Phase 1: central risk gate (additive). Wraps core.check_rules."""
import os
import threading
import time
from datetime import datetime

from flask import jsonify


def _num(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


class Gate:
    def __init__(self, max_trades_day=30, cooldown=0.0, emergency=False, clock=time.time, today=None):
        self.max_trades_day = int(max_trades_day)
        self.cooldown = float(cooldown)
        self.emergency = bool(emergency)
        self.clock = clock
        self.today = today or (lambda: datetime.now().strftime("%Y-%m-%d"))
        self.lock = threading.Lock()
        self.day = self.today()
        self.count = 0
        self.last = 0.0
        self.blocked = 0

    def _roll(self):
        d = self.today()
        if d != self.day:
            self.day, self.count = d, 0

    def check(self):
        with self.lock:
            self._roll()
            now = self.clock()
            msg = None
            if self.emergency:
                msg = "TRADE BLOCKED: EMERGENCY STOP IS ON. Press Resume in the Risk panel."
            elif self.max_trades_day > 0 and self.count >= self.max_trades_day:
                msg = f"TRADE BLOCKED: DAILY TRADE LIMIT REACHED ({self.max_trades_day})."
            elif self.cooldown > 0 and self.last and now - self.last < self.cooldown:
                msg = f"TRADE BLOCKED: COOLDOWN ({int(self.cooldown - (now - self.last)) + 1}s left)."
            if msg:
                self.blocked += 1
            return msg

    def passed(self):
        with self.lock:
            self._roll()
            self.count += 1
            self.last = self.clock()

    def snapshot(self):
        with self.lock:
            self._roll()
            return {"emergency": self.emergency, "trades_today": self.count,
                    "max_trades_day": self.max_trades_day, "cooldown": self.cooldown,
                    "blocked_total": self.blocked, "day": self.day}


def register(core):
    app = core.app
    gate = Gate(
        max_trades_day=_num("RISK_MAX_TRADES_DAY", 30),
        cooldown=_num("RISK_COOLDOWN_SEC", 0),
        emergency=os.getenv("RISK_EMERGENCY_STOP", "").strip().lower() in ("1", "true", "yes"),
        today=lambda: datetime.now(core.TZ).strftime("%Y-%m-%d"),
    )
    original = core.check_rules

    def guarded(broker, symbol, size, side, account):
        msg = gate.check()
        if msg:
            return msg, None
        problem, price = original(broker, symbol, size, side, account)
        if not problem:
            gate.passed()
        return problem, price

    core.check_rules = guarded
    core.risk_gate = gate

    def need():
        _, broker = core.current_session()
        return None if broker else core.denied_response()

    @app.get("/risk/status")
    def risk_status():
        return need() or jsonify(dict(gate.snapshot(), status="success"))

    @app.post("/risk/stop")
    def risk_stop():
        bad = need()
        if bad:
            return bad
        gate.emergency = True
        return jsonify(dict(gate.snapshot(), status="success"))

    @app.post("/risk/resume")
    def risk_resume():
        bad = need()
        if bad:
            return bad
        gate.emergency = False
        return jsonify(dict(gate.snapshot(), status="success"))
