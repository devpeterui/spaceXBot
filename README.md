# SpaceX Bulk Trader

One page, one market, zero cost. Digit and rise/fall trading on your **free Deriv demo account**, with bulk orders, a built-in scanner and an auto bot.

| Needs | Cost |
|---|---|
| Deriv demo token + app id (developers.deriv.com) | free |
| Render free web service | free |
| AI key | **not needed**. The scanner and bot run on built-in statistics |

## Files
- `app.py`: server (login, account, safety rules). Unchanged core.
- `bulk.py`: bulk orders, tick relay, real win/loss results (new).
- `derivs.py`, `extras.py`, `auto.py`: contract builder, charts, server-side auto mode (still available, not used by the page).
- `public/index.html`: the SpaceX Bulk Trader page. It is split into three tabs: Trade (market, digits/candles, scanner, limits), Auto bot (bot settings, limits) and Results. On desktop Results stays in the side column; on phones it is its own tab. `sw.js`, manifest and icons make it installable on a phone.
- Daily loss cap (Limits card): default 5% of the balance you start the day with. No order is sent if it could push the day's loss past the cap, and once it is reached trading stays locked until tomorrow on that device. Resetting the session or raising the percentage does not unlock it.
- Backtest (Auto bot tab): replays digit and rise/fall rules on up to 5000 past ticks of each market and compares them with break-even. It places no orders.
- Markets tab: Run check (feed, account type, daily cap, backtest evidence, session stats) and a weekly brief built from Deriv's hourly candles for all markets. `bulk.py` serves `/bulk/candles` as a fallback when the browser cannot reach Deriv directly. No database is needed: Deriv keeps the week of history.
- Stats tab (Results): your real win rate against break-even, a luck range, and results by market and contract.
- `render.yaml`, `render.env.example`: Render settings and every variable.

## Deploy
1. Push this folder to a **private** GitHub repo. Never commit real keys.
2. Render > New > Web Service > pick the repo. It reads `render.yaml`.
3. In Render > Environment set `ACCESS_CODE` (long random text), `DERIV_TOKEN`, `DERIV_APP_ID`.
4. Open your `.onrender.com` URL and log in with the access code.

## How it works
- Prices come from Deriv's public market-data socket, opened by your browser. If the browser cannot reach it, the page automatically pulls ticks through the server (badge shows "Live · relay").
- Orders go page > `/bulk/execute` > Deriv. Win/loss and profit come back from Deriv (`/bulk/results`), not from a simulation.
- Limits (take profit, stop loss, max trades) are checked in the page. The server also enforces demo-only, `MAX_STAKE`, `MAX_BULK`, daily loss and trading hours.

## Safety
- `ALLOW_LIVE=false` blocks every non-demo account. The bot is demo-only.
- `MAX_STAKE` caps each contract. `MAX_BULK` caps contracts per click (hard maximum 20).
- Martingale defaults to x1 (off). Digits on synthetic markets are random: the scanner cannot give an edge, and raising stakes after losses raises risk.
- Render's free server sleeps. Session counters and the daily-loss baseline reset when it restarts.
- MetaAPI (paid) and Binance code is still in `app.py` but only activates if you set those keys. The page uses Deriv only.
