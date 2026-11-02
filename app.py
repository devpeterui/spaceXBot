"""VibeTrade AI bridge: three brokers, one dashboard.

  metaapi : MT5 forex through MetaAPI (paid, hourly)
  deriv   : Deriv options/multipliers (free demo, phone-only)
  binance : Binance spot, testnet by default (free)

Only brokers whose keys are set in the environment show up on the login screen.
All secrets come from environment variables. Nothing is hard-coded.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # real client IP behind Render's proxy


# ---------------------------------------------------------------- settings
def env_str(name, default=""):
    return (os.getenv(name) or default).strip()


def env_float(name, default=0.0):
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_int(name, default=0):
    try:
        return int(float(env_str(name, str(default))))
    except ValueError:
        return default


def env_bool(name, default=False):
    return env_str(name, str(default)).lower() in ("1", "true", "yes", "on")


ACCESS_CODE = env_str("ACCESS_CODE")
TIMEZONE = env_str("TIMEZONE", "UTC")
DEFAULT_BROKER = env_str("BROKER", "metaapi").lower()
if DEFAULT_BROKER == "cloud":  # old name
    DEFAULT_BROKER = "metaapi"

# MetaAPI (MT5)
METAAPI_TOKEN = env_str("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = env_str("METAAPI_ACCOUNT_ID")
METAAPI_REGION = env_str("METAAPI_REGION", "new-york")
# Deriv
DERIV_TOKEN = env_str("DERIV_TOKEN")
DERIV_APP_ID = env_str("DERIV_APP_ID")
DERIV_ACCOUNT_ID = env_str("DERIV_ACCOUNT_ID")  # optional default account; the page can switch between your demo and real accounts
# Binance
BINANCE_API_KEY = env_str("BINANCE_API_KEY")
BINANCE_API_SECRET = env_str("BINANCE_API_SECRET")
BINANCE_ENV = env_str("BINANCE_ENV", "testnet").lower()  # testnet | live

# Safety limits
ALLOW_LIVE = env_bool("ALLOW_LIVE", True)   # real accounts allowed; the loss caps below still apply to them
MAX_LOT = env_float("MAX_LOT", 0.10)               # metaapi: lots per order
MAX_STAKE = env_float("MAX_STAKE", 1)               # deriv: stake per contract
MAX_ORDER_USDT = env_float("MAX_ORDER_USDT", 100)    # binance: order value in USDT
MAX_DAILY_LOSS_PCT = env_float("MAX_DAILY_LOSS_PCT", 3)
MAX_POSITIONS = env_int("MAX_POSITIONS", 3)         # metaapi only
DAILY_PROFIT_TARGET = env_float("DAILY_PROFIT_TARGET", 0)  # account currency, 0 = off
DAILY_LOSS_LIMIT = env_float("DAILY_LOSS_LIMIT", 0)        # account currency, 0 = off
TRADE_START_HOUR = env_int("TRADE_START_HOUR", 0)
TRADE_END_HOUR = env_int("TRADE_END_HOUR", 24)
WEEKDAYS_ONLY = env_bool("WEEKDAYS_ONLY", True)
PRICE_MIN = env_float("PRICE_MIN", 0)               # metaapi and binance
PRICE_MAX = env_float("PRICE_MAX", 0)

# AI chat
AI_PROVIDER = env_str("AI_PROVIDER", "gemini").lower()  # gemini | groq | xai
AI_API_KEY = env_str("AI_API_KEY")
AI_MODEL = env_str("AI_MODEL")

DEFAULT_CODE_PLACEHOLDER = "CHANGE-ME"
SESSION_SECONDS = 12 * 3600
SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.#-]{1,24}$")

try:
    TZ = ZoneInfo(TIMEZONE)
except Exception:  # bad TIMEZONE value: fall back instead of crashing
    TZ = ZoneInfo("UTC")


# ---------------------------------------------------------------- errors / http
class BrokerError(Exception):
    def __init__(self, message, http=502):
        super().__init__(message)
        self.message = message
        self.http = http


def call(method, url, label, **kwargs):
    """HTTP request with friendly errors. Returns (response, json_or_None)."""
    try:
        r = requests.request(method, url, timeout=30, **kwargs)
    except requests.RequestException:
        raise BrokerError(f"Cannot reach {label}. Try again in a moment.", 503)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r, body


# ---------------------------------------------------------------- brokers
class MetaApi:
    id = "metaapi"
    label = "MT5 forex (MetaAPI)"
    size_label = "Volume (lots)"
    default_symbol = "EURUSD"
    buy_label, sell_label = "BUY", "SELL"
    needs_duration = False
    supports_price = True

    def configured(self):
        return bool(METAAPI_TOKEN and METAAPI_ACCOUNT_ID)

    def round_size(self, v):
        return round(v, 2)

    def _call(self, method, path, **kw):
        url = (
            f"https://mt-client-api-v1.{METAAPI_REGION}.agiliumtrade.ai"
            f"/users/current/accounts/{METAAPI_ACCOUNT_ID}{path}"
        )
        r, body = call(method, url, "MetaAPI", headers={"auth-token": METAAPI_TOKEN}, **kw)
        if r.status_code == 202:
            raise BrokerError("MetaAPI account is still connecting. Wait 30 seconds and retry.", 503)
        if r.status_code in (401, 403):
            raise BrokerError("MetaAPI rejected the token. Check METAAPI_TOKEN and METAAPI_REGION.", 503)
        if r.status_code == 404:
            raise BrokerError("MetaAPI account not found. Check METAAPI_ACCOUNT_ID and METAAPI_REGION.", 503)
        if r.status_code >= 400:
            msg = body.get("message") if isinstance(body, dict) else None
            raise BrokerError(msg or f"MetaAPI error {r.status_code}", 502)
        return body

    def get_account(self):
        a = self._call("GET", "/account-information") or {}
        server = str(a.get("server", ""))
        kind = str(a.get("type", "")).upper()
        return {
            "demo": "DEMO" in kind or "demo" in server.lower(),
            "name": a.get("name") or "Account",
            "login": a.get("login", ""),
            "server": server,
            "balance": float(a.get("balance", 0) or 0),
            "equity": float(a.get("equity", 0) or 0),
            "currency": a.get("currency", ""),
        }

    def size_error(self, size):
        return f"Volume {size} is above MAX_LOT {MAX_LOT}." if size > MAX_LOT else None

    def count_positions(self):
        positions = self._call("GET", "/positions")
        return len(positions) if isinstance(positions, list) else 0

    def quote(self, symbol, side):
        q = self._call("GET", f"/symbols/{symbol}/current-price") or {}
        return q.get("ask") if side == "buy" else q.get("bid")

    def extra_check(self, size, price):
        return None

    def place(self, side, symbol, size, duration):
        res = self._call(
            "POST",
            "/trade",
            json={
                "actionType": "ORDER_TYPE_BUY" if side == "buy" else "ORDER_TYPE_SELL",
                "symbol": symbol,
                "volume": size,
                "comment": "VibeTrade",
            },
        ) or {}
        code = res.get("stringCode", "")
        if code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED", "TRADE_RETCODE_DONE_PARTIAL"):
            return res.get("orderId") or res.get("positionId") or "", None
        raise BrokerError(res.get("message") or code or "Order rejected", 502)


class Deriv:
    id = "deriv"
    label = "Deriv options"
    size_label = "Stake"
    default_symbol = "1HZ100V"
    buy_label, sell_label = "RISE", "FALL"
    needs_duration = True
    supports_price = False
    API = "https://api.derivws.com/trading/v1/options"

    def configured(self):
        return bool(DERIV_TOKEN and DERIV_APP_ID)

    def round_size(self, v):
        return round(v, 2)

    def _headers(self):
        return {"Authorization": f"Bearer {DERIV_TOKEN}", "Deriv-App-ID": DERIV_APP_ID}

    def _accounts(self):
        r, body = call("GET", f"{self.API}/accounts", "Deriv", headers=self._headers())
        if r.status_code in (401, 403):
            raise BrokerError("Deriv rejected the token. Check DERIV_TOKEN and DERIV_APP_ID.", 503)
        if r.status_code >= 400 or not isinstance(body, dict):
            raise BrokerError(f"Deriv accounts error {r.status_code}", 502)
        return body.get("data") or []

    def _pick(self):
        accounts = [a for a in self._accounts() if a.get("status", "active") == "active"]
        chosen = _chosen_account_id()          # picked in the page for this login session
        if chosen:
            for a in accounts:
                if a.get("account_id") == chosen:
                    return a
        if DERIV_ACCOUNT_ID:
            for a in accounts:
                if a.get("account_id") == DERIV_ACCOUNT_ID:
                    return a
            raise BrokerError("DERIV_ACCOUNT_ID was not found among your active Deriv accounts.", 503)
        for a in accounts:                      # default: demo first, so a fresh login starts on the safe side
            if a.get("account_type") == "demo":
                return a
        if accounts:
            return accounts[0]
        raise BrokerError("No active Deriv account found.", 503)

    def get_account(self):
        a = self._pick()
        bal = float(a.get("balance", 0) or 0)
        return {
            "demo": a.get("account_type") == "demo",
            "name": "Deriv " + str(a.get("account_type", "")),
            "login": a.get("account_id", ""),
            "server": "Deriv options",
            "balance": bal,
            "equity": bal,
            "currency": a.get("currency", ""),
            "_raw": a,
        }

    def size_error(self, size):
        return f"Stake {size} is above MAX_STAKE {MAX_STAKE}." if size > MAX_STAKE else None

    def count_positions(self):
        return None

    def quote(self, symbol, side):
        return None

    def extra_check(self, size, price):
        return None

    def _ws_send(self, ws, payload, req_id):
        ws.send(json.dumps(dict(payload, req_id=req_id)))
        for _ in range(12):
            msg = json.loads(ws.recv())
            if msg.get("req_id") != req_id:
                continue
            if msg.get("error"):
                raise BrokerError("Deriv: " + str(msg["error"].get("message", msg["error"])), 502)
            return msg
        raise BrokerError("Deriv did not answer in time.", 504)

    def place(self, side, symbol, size, duration):
        import websocket  # websocket-client

        acct = self._pick()
        r, body = call(
            "POST", f"{self.API}/accounts/{acct.get('account_id')}/otp", "Deriv", headers=self._headers()
        )
        if r.status_code >= 400 or not isinstance(body, dict):
            raise BrokerError(f"Deriv could not open a trading session ({r.status_code}).", 502)
        url = (body.get("data") or {}).get("url")
        if not url:
            raise BrokerError("Deriv did not return a trading session URL.", 502)
        try:
            ws = websocket.create_connection(url, timeout=20)
        except Exception:
            raise BrokerError("Cannot connect to Deriv's trading socket.", 503)
        try:
            prop = self._ws_send(
                ws,
                {
                    "proposal": 1,
                    "amount": size,
                    "basis": "stake",
                    "contract_type": "CALL" if side == "buy" else "PUT",
                    "currency": acct.get("currency", "USD"),
                    "duration": int(duration),
                    "duration_unit": "m",
                    "underlying_symbol": symbol,
                },
                1,
            ).get("proposal") or {}
            if not prop.get("id"):
                raise BrokerError("Deriv returned no proposal. Check the market name and duration.", 502)
            buy = self._ws_send(
                ws, {"buy": prop["id"], "price": prop.get("ask_price", size)}, 2
            ).get("buy") or {}
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("Deriv connection dropped. Try again.", 503)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return buy.get("contract_id", ""), buy.get("buy_price", size)


class Binance:
    id = "binance"
    size_label = "Quantity (coin)"
    default_symbol = "BTCUSDT"
    buy_label, sell_label = "BUY", "SELL"
    needs_duration = False
    supports_price = True

    @property
    def label(self):
        return "Binance testnet (free)" if BINANCE_ENV != "live" else "Binance LIVE"

    @property
    def base(self):
        return "https://api.binance.com" if BINANCE_ENV == "live" else "https://testnet.binance.vision"

    def configured(self):
        return bool(BINANCE_API_KEY and BINANCE_API_SECRET)

    def round_size(self, v):
        return round(v, 8)

    def _signed(self, method, path, params=None):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        qs = urlencode(p)
        sig = hmac.new(BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        r, body = call(
            method, f"{self.base}{path}?{qs}&signature={sig}", "Binance",
            headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        )
        if r.status_code >= 400:
            msg = body.get("msg") if isinstance(body, dict) else None
            raise BrokerError("Binance: " + (msg or f"error {r.status_code}"), 502)
        return body

    def _price(self, symbol):
        r, body = call("GET", f"{self.base}/api/v3/ticker/price", "Binance", params={"symbol": symbol})
        if r.status_code >= 400 or not isinstance(body, dict):
            raise BrokerError(f"Binance has no price for {symbol}.", 502)
        return float(body["price"])

    def get_account(self):
        acct = self._signed("GET", "/api/v3/account") or {}
        r, prices = call("GET", f"{self.base}/api/v3/ticker/price", "Binance")
        table = {p["symbol"]: float(p["price"]) for p in prices} if isinstance(prices, list) else {}
        total = 0.0
        usdt = 0.0
        for b in acct.get("balances", []):
            qty = float(b.get("free", 0)) + float(b.get("locked", 0))
            if qty <= 0:
                continue
            asset = b.get("asset", "")
            if asset == "USDT":
                usdt = qty
                total += qty
            elif asset + "USDT" in table:
                total += qty * table[asset + "USDT"]
        return {
            "demo": BINANCE_ENV != "live",
            "name": "Binance " + ("testnet" if BINANCE_ENV != "live" else "LIVE"),
            "login": "key ..." + BINANCE_API_KEY[-4:],
            "server": self.base.replace("https://", ""),
            "balance": usdt,
            "equity": total,
            "currency": "USDT",
        }

    def size_error(self, size):
        return None

    def count_positions(self):
        return None

    def quote(self, symbol, side):
        return self._price(symbol)

    def extra_check(self, size, price):
        if price and size * price > MAX_ORDER_USDT:
            return f"Order value {size * price:.2f} USDT is above MAX_ORDER_USDT {MAX_ORDER_USDT}."
        return None

    def place(self, side, symbol, size, duration):
        qty = format(Decimal(str(size)).normalize(), "f")
        res = self._signed(
            "POST", "/api/v3/order",
            {"symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": qty},
        ) or {}
        executed = float(res.get("executedQty", 0) or 0)
        spent = float(res.get("cummulativeQuoteQty", 0) or 0)
        price = round(spent / executed, 8) if executed else None
        return res.get("orderId", ""), price


BROKERS = {b.id: b for b in (MetaApi(), Deriv(), Binance())}


def enabled_brokers():
    return [b for b in BROKERS.values() if b.configured()]


# ---------------------------------------------------------------- sessions
_lock = threading.Lock()
_sessions = {}        # token -> (expiry, broker id)
_failed_logins = {}   # ip -> [timestamps]
_acct_choice = {}     # session token -> Deriv account id picked in the page


def _chosen_account_id():
    """Account the logged-in page picked (demo or real). None outside a request."""
    try:
        header = request.headers.get("Authorization", "")
    except RuntimeError:
        return None
    if not header.startswith("Bearer "):
        return None
    with _lock:
        return _acct_choice.get(header[7:].strip())
_baseline = {}        # (broker id, "YYYY-MM-DD") -> equity at first read of that day


def new_session(broker_id):
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for t in [t for t, (exp, _) in _sessions.items() if exp < now]:
            del _sessions[t]
        _sessions[token] = (now + SESSION_SECONDS, broker_id)
    return token


def current_session():
    """Return (token, broker) or (None, None)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    token = header[7:].strip()
    with _lock:
        entry = _sessions.get(token)
        if entry and entry[0] > time.time() and entry[1] in BROKERS:
            return token, BROKERS[entry[1]]
        _sessions.pop(token, None)
    return None, None


def too_many_failures(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _failed_logins.get(ip, []) if now - t < 600]
        _failed_logins[ip] = recent
        return len(recent) >= 5


def record_failure(ip):
    with _lock:
        _failed_logins.setdefault(ip, []).append(time.time())


def denied_response():
    return jsonify({"status": "error", "message": "Session expired, log in again"}), 401


def public_account(a):
    return {k: v for k, v in a.items() if not k.startswith("_")}


# ---------------------------------------------------------------- rules
def check_rules(broker, symbol, size, side, account):
    """Return (error_or_None, quote_price_or_None)."""
    if not account["demo"] and not ALLOW_LIVE:
        return "Real accounts are switched off on this server (ALLOW_LIVE=false).", None
    problem = broker.size_error(size)
    if problem:
        return problem, None

    now = datetime.now(TZ)
    if WEEKDAYS_ONLY and now.weekday() >= 5 and broker.id == "metaapi":
        return "Forex is off at weekends (WEEKDAYS_ONLY=true).", None
    if not (TRADE_START_HOUR <= now.hour < TRADE_END_HOUR):
        return f"Outside trading hours ({TRADE_START_HOUR}:00 to {TRADE_END_HOUR}:00 {TIMEZONE}).", None

    login_id = str(account.get("login", ""))
    key = (broker.id, login_id, now.strftime("%Y-%m-%d"))
    with _lock:
        for old in [k for k in _baseline if k != key and k[0] == broker.id and k[1] == login_id]:
            del _baseline[old]
        start_equity = _baseline.setdefault(key, account["equity"])
    change = account["equity"] - start_equity
    if DAILY_LOSS_LIMIT > 0 and -change >= DAILY_LOSS_LIMIT:
        return f"Daily loss limit reached ({-change:.2f} {account['currency']}).", None
    if MAX_DAILY_LOSS_PCT > 0 and start_equity > 0 and -change / start_equity * 100 >= MAX_DAILY_LOSS_PCT:
        return f"Daily loss limit of {MAX_DAILY_LOSS_PCT}% reached.", None
    if DAILY_PROFIT_TARGET > 0 and change >= DAILY_PROFIT_TARGET:
        return f"Daily profit target reached (+{change:.2f} {account['currency']}).", None

    if MAX_POSITIONS > 0:
        n = broker.count_positions()
        if n is not None and n >= MAX_POSITIONS:
            return f"Already {n} open positions (MAX_POSITIONS={MAX_POSITIONS}).", None

    price = None
    try:
        price = broker.quote(symbol, side)
    except BrokerError as e:
        if broker.supports_price and (PRICE_MIN > 0 or PRICE_MAX > 0 or broker.id == "binance"):
            return f"Could not read the price for {symbol}: {e.message}", None
    if price is not None and broker.supports_price:
        if PRICE_MIN > 0 and price < PRICE_MIN:
            return f"Price {price} is below PRICE_MIN {PRICE_MIN}.", None
        if PRICE_MAX > 0 and price > PRICE_MAX:
            return f"Price {price} is above PRICE_MAX {PRICE_MAX}.", None
    problem = broker.extra_check(size, price)
    if problem:
        return problem, None
    return None, price


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/config")
def config():
    ready = enabled_brokers()
    default = DEFAULT_BROKER if DEFAULT_BROKER in [b.id for b in ready] else (ready[0].id if ready else "")
    return jsonify(
        {
            "default": default,
            "brokers": [
                {
                    "id": b.id,
                    "label": b.label,
                    "size_label": b.size_label,
                    "default_symbol": b.default_symbol,
                    "buy_label": b.buy_label,
                    "sell_label": b.sell_label,
                    "needs_duration": b.needs_duration,
                }
                for b in ready
            ],
        }
    )


@app.post("/login")
def login():
    ip = request.remote_addr or "?"
    if too_many_failures(ip):
        return jsonify({"status": "error", "message": "Too many attempts. Wait 10 minutes."}), 429
    if not ACCESS_CODE or ACCESS_CODE.startswith(DEFAULT_CODE_PLACEHOLDER):
        return jsonify({"status": "error", "message": "Server ACCESS_CODE is not set yet."}), 503
    data = request.get_json(silent=True) or {}
    given = str(data.get("access_code", ""))
    if not hmac.compare_digest(given.encode(), ACCESS_CODE.encode()):
        record_failure(ip)
        return jsonify({"status": "error", "message": "Wrong access code."}), 401

    ready = {b.id: b for b in enabled_brokers()}
    if not ready:
        return jsonify({"status": "error", "message": "No broker is set up yet. Add its keys in Render > Environment."}), 503
    wanted = str(data.get("broker") or DEFAULT_BROKER)
    broker = ready.get(wanted) or next(iter(ready.values()))
    try:
        account = broker.get_account()
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http
    return jsonify(
        {
            "status": "success",
            "token": new_session(broker.id),
            "broker": broker.id,
            "account": public_account(account),
        }
    )


@app.post("/logout")
def logout():
    token, _ = current_session()
    if token:
        with _lock:
            _sessions.pop(token, None)
            _acct_choice.pop(token, None)
    return jsonify({"status": "success"})


@app.get("/account")
def account():
    _, broker = current_session()
    if not broker:
        return denied_response()
    try:
        return jsonify({"status": "success", "account": public_account(broker.get_account())})
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http


@app.get("/deriv/accounts")
def deriv_accounts():
    """Every active account behind your Deriv token (demo and real) so the page can switch."""
    _, broker = current_session()
    if not broker:
        return denied_response()
    if broker.id != "deriv":
        return jsonify({"status": "success", "accounts": [], "current": ""})
    try:
        rows = [a for a in broker._accounts() if a.get("status", "active") == "active"]
        current = str(broker.get_account().get("login", ""))
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http
    out = []
    for a in rows:
        if a.get("account_type") != "demo" and not ALLOW_LIVE:
            continue
        out.append({"id": a.get("account_id"), "type": a.get("account_type"),
                    "balance": float(a.get("balance", 0) or 0), "currency": a.get("currency", "")})
    return jsonify({"status": "success", "accounts": out, "current": current})


@app.post("/deriv/select")
def deriv_select():
    """Switch this login session to another of your Deriv accounts (demo or real)."""
    token, broker = current_session()
    if not broker:
        return denied_response()
    if broker.id != "deriv":
        return jsonify({"status": "error", "message": "Account switching is for Deriv."}), 400
    wanted = str((request.get_json(silent=True) or {}).get("account_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", wanted):
        return jsonify({"status": "error", "message": "Invalid account."}), 400
    try:
        rows = [a for a in broker._accounts() if a.get("status", "active") == "active"]
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http
    match = next((a for a in rows if a.get("account_id") == wanted), None)
    if not match:
        return jsonify({"status": "error", "message": "That account is not active on your Deriv token."}), 404
    if match.get("account_type") != "demo" and not ALLOW_LIVE:
        return jsonify({"status": "error", "message": "Real accounts are switched off (ALLOW_LIVE=false)."}), 403
    with _lock:
        _acct_choice[token] = wanted
    try:
        return jsonify({"status": "success", "account": public_account(broker.get_account())})
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http


@app.post("/execute")
def execute():
    _, broker = current_session()
    if not broker:
        return denied_response()
    data = request.get_json(silent=True) or {}
    side = str(data.get("side", "")).lower()
    symbol = str(data.get("symbol", "")).strip()
    if broker.id == "binance":
        symbol = symbol.upper()
    try:
        size = broker.round_size(float(data.get("volume", 0)))
    except (TypeError, ValueError):
        size = 0
    try:
        duration = int(data.get("duration") or 5)
    except (TypeError, ValueError):
        duration = 5
    if side not in ("buy", "sell"):
        return jsonify({"status": "error", "message": "side must be buy or sell"}), 400
    if not SYMBOL_RE.match(symbol):
        return jsonify({"status": "error", "message": "Invalid symbol"}), 400
    if not size > 0:
        return jsonify({"status": "error", "message": "Size must be above 0"}), 400
    if broker.needs_duration and not 1 <= duration <= 1440:
        return jsonify({"status": "error", "message": "Duration must be 1 to 1440 minutes"}), 400

    try:
        acct = broker.get_account()
        problem, quote = check_rules(broker, symbol, size, side, acct)
        if problem:
            return jsonify({"status": "error", "message": problem}), 403
        order, price = broker.place(side, symbol, size, duration)
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http
    if price is None:
        price = quote if quote is not None else "market"
    return jsonify({"status": "success", "order": order, "price": price})


# ---------------------------------------------------------------- AI chat
AI_SYSTEM = (
    "You are a concise trading assistant inside a small dashboard. You cannot place "
    "trades or see the account. Give educational answers, mention risk briefly, and "
    "never present anything as financial advice."
)
OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    "xai": ("https://api.x.ai/v1/chat/completions", "grok-4"),
}
GEMINI_DEFAULT_MODEL = "gemini-flash-lite-latest"


def ask_ai(message):
    if not AI_API_KEY:
        raise BrokerError("AI chat is not set up (AI_API_KEY is empty).", 503)
    try:
        if AI_PROVIDER == "gemini":
            model = AI_MODEL or GEMINI_DEFAULT_MODEL
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": AI_API_KEY},
                json={
                    "systemInstruction": {"parts": [{"text": AI_SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": message}]}],
                },
                timeout=40,
            )
            if r.status_code >= 400:
                raise BrokerError(f"AI provider error {r.status_code}. Check AI_API_KEY, AI_MODEL and your quota.")
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        if AI_PROVIDER in OPENAI_COMPAT:
            url, default_model = OPENAI_COMPAT[AI_PROVIDER]
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={
                    "model": AI_MODEL or default_model,
                    "messages": [
                        {"role": "system", "content": AI_SYSTEM},
                        {"role": "user", "content": message},
                    ],
                },
                timeout=40,
            )
            if r.status_code >= 400:
                raise BrokerError(f"AI provider error {r.status_code}. Check AI_API_KEY, AI_MODEL and your quota.")
            return r.json()["choices"][0]["message"]["content"]
    except requests.RequestException:
        raise BrokerError("Cannot reach the AI provider.", 503)
    except (KeyError, IndexError, ValueError):
        raise BrokerError("AI provider sent an unexpected reply.", 502)
    raise BrokerError("AI_PROVIDER must be gemini, groq or xai.", 503)


@app.post("/chat")
def chat():
    _, broker = current_session()
    if not broker:
        return denied_response()
    message = str((request.get_json(silent=True) or {}).get("message", "")).strip()[:1000]
    if not message:
        return jsonify({"status": "error", "message": "Type a question first."}), 400
    try:
        return jsonify({"status": "success", "reply": ask_ai(message)})
    except BrokerError as e:
        return jsonify({"status": "error", "message": e.message}), e.http


# ---------------------------------------------------------------- front end
@app.get("/")
@app.get("/index.html")
def index():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(PUBLIC_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(PUBLIC_DIR, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/icon-<size>.png")
def icon(size):
    if size not in ("192", "512"):
        return ("", 404)
    return send_from_directory(PUBLIC_DIR, f"icon-{size}.png")


@app.get("/favicon.svg")
def favicon_svg():
    resp = send_from_directory(PUBLIC_DIR, "favicon.svg", mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/favicon.ico")
def favicon_ico():
    return send_from_directory(PUBLIC_DIR, "icon-192.png", mimetype="image/png")


# ---------------------------------------------------------------- extras
# Charts, trade history and AI signal live in extras.py. If it fails to load,
# the bot keeps working exactly as before.
try:
    import sys as _sys

    import extras

    extras.register(_sys.modules[__name__])
except Exception as _err:
    print("extras not loaded:", _err)


try:
    import derivs

    derivs.register(_sys.modules[__name__])
except Exception as _err:
    print("derivs not loaded:", _err)


try:
    import bulk

    bulk.register(_sys.modules[__name__])
except Exception as _err:
    print("bulk not loaded:", _err)


try:
    import auto

    auto.register(_sys.modules[__name__])
except Exception as _err:
    print("auto not loaded:", _err)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
