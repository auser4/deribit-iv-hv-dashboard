# deribit-iv-hv-dashboard
Personal Option Gex Dashboard SRE
# Deribit Options Analytics Dashboard

A local, dependency-free Python dashboard that turns Deribit's public options data into dealer-positioning and volatility analytics for BTC and ETH. Built for personal options research; runs entirely on your machine and needs no API key for market data.

> **Disclaimer:** research tool only. Outputs are model-based estimates, not trading or financial advice.

## What it does

**Gamma and positioning**
- Gamma exposure (GEX) by strike and by expiry, plus net/call/put gamma and a bias label
- Delta, vanna and charm exposure
- Gamma flip level (zero-GEX), key support/resistance/accelerator strikes, nearest pin strike
- Open-interest changes between snapshots and expiry concentration buckets

**Volatility**
- 25-delta risk reversal, butterfly and skew per expiry (vol surface)
- 7-day realized vol vs. ATM implied vol (IV − RV premium)
- Realized vol-of-vol and spot/vol correlation, with a local history bootstrap
- Skew and vol-of-vol risk premium proxies

**Market regime model**
- Explainable score built from price momentum, price × perp OI interaction, option sentiment, gamma structure, volume confirmation, Deribit taker imbalance and funding crowding
- Separate direction and acceleration scores, with hysteresis so the label does not flip on a single snapshot
- Forward-outcome tracking (1h / 4h / 24h) so the score can be checked against what the market actually did

**Optional AI summary**
- A short plain-language summary of the current snapshot, generated on the backend only. Enabled by setting `OPENAI_API_KEY`; skipped otherwise.

## Method and assumptions

- Greeks use Black-Scholes with `r = 0`.
- GEX assumes customers are net long options, so dealers are short calls and long puts. This is a modelling assumption, not verified dealer positioning.
- Taker imbalance comes from recent Deribit perpetual trades only. It is not global CVD.
- Option volume and OI do not reveal who initiated a trade, so they carry no directional score.
- Cross-exchange liquidations are not covered.

## Requirements

- Python 3.8+
- No third-party packages (standard library only)

## Run

```bash
python server.py
```

Then open <http://127.0.0.1:8000>.

The server binds to `127.0.0.1` only. API calls to Deribit go directly from the Python process, and the browser only talks to the local server.

Optional environment variables:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Enables the backend-only AI summary |
| `OPENAI_MODEL` | Model for the summary (default `gpt-4o-mini`) |

## API (local)

| Endpoint | Returns |
|---|---|
| `/api/snapshot?asset=BTC` | Full analytics snapshot (`BTC` or `ETH`), cached for 60 seconds |
| `/api/history?asset=BTC` | Up to 365 daily saved snapshots |
| `/api/evaluation?asset=BTC` | Hit rate and average move of past regime signals by horizon |

## Data storage

Snapshots are stored in a local SQLite file, `deribit_snapshots.sqlite3`, created on first run. A background scheduler saves one snapshot per asset daily at 00:00 UTC while the server is running. Realized-vol and spot history are backfilled once from Deribit's public API.

## Project structure

```
.
├── server.py        # backend: data fetching, analytics, SQLite, HTTP server
├── index.html       # dashboard UI served by server.py
└── README.md
```

## Status

Personal research project, in active development. Some metrics (regime evaluation, implied-vol history) need several days of local snapshots before they become meaningful; the dashboard labels these as "accumulating".
