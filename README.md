# Signal Scanner

Cloud-hosted scanner that runs your "Fabio + Carmine + Marco + Andrea Master Engine v8"
rules (ported from Pine to Python) on live market data, 24/7, with a web dashboard,
browser alerts and optional Telegram alerts.

## Run it locally (demo data, no keys)
    pip install -r requirements.txt
    python server.py            # http://127.0.0.1:8000

Demo mode uses synthetic prices. Signals in demo mode mean nothing.

## Use real data
Edit `config.json` -> `"provider"` and set the key as an environment variable.

| provider     | env vars                          | notes |
|--------------|-----------------------------------|-------|
| `oanda`      | `OANDA_TOKEN`, `OANDA_ENV=practice|live` | Tick volume for forex, gold, oil, index CFDs. Best fit for this strategy. A free practice account works for data. |
| `twelvedata` | `TWELVEDATA_KEY`                  | Spot forex/metals usually return NO volume. Free plan: 8 requests/min, so ~4 symbols on 1-min bars. Indices need a paid plan. |

**Volume matters:** RVOL, CVD and the volume profile all need volume. If a feed has none,
the dashboard shows a warning and the strategy cannot trigger.

Symbols, bar interval and every engine parameter (`"params": {...}`) are in `config.json`.
The script's defaults assume **1-minute bars** (390-bar profile = one US cash session).

## Deploy (so it runs while your devices are off)
Any host that runs a Docker container or Python: Render, Railway, Fly.io, a VPS.
1. Push this folder to a Git repo and deploy it (Dockerfile included).
2. Set env vars: `ACCESS_TOKEN` (required, the dashboard asks for it), your data key, and
   optionally `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
3. Keep it to ONE worker/instance (otherwise alerts duplicate). The host must stay on
   (free tiers that sleep will miss signals). Mount a volume and set `DB_PATH` if you want
   signal history to survive redeploys.

## Alerts
* Dashboard: press "Enable alerts" for a notification + beep. Works only while the page is open.
* Telegram: works with everything closed. Create a bot with @BotFather, set the two env vars.
* True background browser push (works with the page closed, no Telegram) needs `pywebpush`
  and a service worker; not included yet.


## Connect MetaTrader 5 (EA mode)
The server is the signal brain; the EA in `mt5/AuctionSignalEA.mq5` is the hands. The EA polls
`/api/ea/poll` every few seconds, and places the trade on your MT5 account. The dashboard shows
"EA: connected" while it is polling.

1. Deploy the server (above) so it is online 24/7. The EA cannot read the published Claude page,
   only this server.
2. Copy `mt5/AuctionSignalEA.mq5` into MT5: File > Open Data Folder > MQL5 > Experts. Open it in
   MetaEditor and press Compile (F7). Fix any compile error before going on.
3. In MT5: Tools > Options > Expert Advisors > tick "Allow WebRequest for listed URL" and add your
   server address (for example https://your-app.onrender.com).
4. Attach the EA to ANY chart. Set `ServerUrl` and `AccessToken` (same as the server's ACCESS_TOKEN).
   Edit `SymbolMap` to your broker's names (suffixes such as EURUSD.m are common).
5. Leave `DryRun = true` first. The Experts tab logs what it WOULD do on every signal. Only switch
   it off on a demo account once those logs match the dashboard.

What the EA does: sizes the lot so the stop risks `RiskPercent` of balance (rounded down, skips if
the minimum lot would risk more), uses the signal's stop and TP2 distances from ITS fill price,
closes `PartialPercent` at TP1 and moves the stop to break-even, and stops opening trades after
`MaxTradesPerDay` or `MaxDailyLossPct`. It refuses to start on a real account unless `AllowLive`
is true. It never trades signals that already existed when it first started, or any older than
`MaxSignalAgeSec`.

Be aware of:
* **Not compiled or tested.** I could not run MetaEditor or a broker connection. Treat it as a
  first draft, and read the Experts log in dry-run before anything else.
* **Delay.** The server checks a few seconds after each bar closes, the EA polls every
  `PollSeconds`, so entries happen seconds after the signal bar and at a different price than the
  data feed shows. Only the distances are carried over, not the entry price.
* **Symbol and spread differences** between the data feed and your broker can change the result.
  Backtest numbers do not include spread or slippage.
* One position per symbol at a time. Hedging and netting accounts are both handled the same way.

## Backtest
    python backtest.py --provider oanda --symbol EUR/USD --bars 100000
    python backtest.py --csv my_bars.csv --symbol EUR/USD
Assumptions are in the file header (stop assumed first if SL and TP hit in one bar; no
spread/slippage unless `--cost-r`). Use 100+ trades before trusting any number.

## Things I found in the original Pine script (kept as-is in the port)
1. **Monster Rebound never turns on.** `inReboundMode` is never set to true, so the 3.0R
   target never applies.
2. **"90% confidence" is a label, not a statistic.** With Ultra mode on, the A+ test is always
   true, so every signal is graded A+. Nothing in the script measures win rate.
3. **TP2 can be nearer than TP1.** TP2 uses the previous day's POC when it is beyond price,
   which can sit between entry and TP1. These are flagged in the dashboard/Telegram.
4. The `Telegram Chat ID` input in the Pine script is not used by it, so it is not copied here.

## Not verified
* Live calls to OANDA / Twelve Data / Telegram were not run (no network in the build
  environment). Test with a practice account first.
* The Python port has not been compared bar-for-bar against TradingView. Before trusting
  it, compare the first few signals with your chart (same symbol, 1m, same time zone).
  Small differences are possible (e.g. EMA/RMA seeding, "new day" boundary).
