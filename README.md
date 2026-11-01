# VibeTrade AI bot

One dashboard, three markets. Only the ones whose keys you set appear on the login screen.

| Market | Cost | Size field | Keys |
|---|---|---|---|
| Binance spot (testnet by default) | free | quantity of coin | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| Deriv options (demo) | free | stake + minutes | `DERIV_TOKEN`, `DERIV_APP_ID` |
| MT5 forex via MetaAPI | paid, hourly | lots | `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` |

## Files
- `app.py`: the server (login, account, execute, optional AI chat)
- `public/`: dashboard, service worker, manifest, icons
- `render.yaml`: Render settings
- `render.env.example`: every environment variable, with placeholders

## Deploy
1. Push this folder's contents to a **private** GitHub repo. Never commit real keys.
2. Render: New > Web Service > pick the repo. Language Python 3, build `pip install -r requirements.txt`, start `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60`.
3. Add the env vars in Render > Environment, then open your `.onrender.com` URL.

## Safety
- `ALLOW_LIVE=false` blocks every non-demo account. Binance stays on testnet unless `BINANCE_ENV=live`.
- Size limits: `MAX_LOT` (MT5), `MAX_STAKE` (Deriv), `MAX_ORDER_USDT` (Binance).
- `MAX_POSITIONS` and `PRICE_MIN/MAX` apply only where the broker supports them (MT5; price limits also Binance).
- Daily loss/profit limits compare against the first equity seen that day. Render's free server sleeps and restarts, which resets that starting point.
- `MAX_RISK_PCT` is not used: orders have no stop loss.
