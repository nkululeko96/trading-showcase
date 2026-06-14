# Trading Systems Showcase

A curated, public extract of work from my private production trading monorepo.
I design, deploy and operate live algorithmic trading systems with my own
capital across crypto-native venues — perpetual futures, prediction markets,
options and CFDs. The full codebase stays private because it contains live
strategy logic; this repo shows the *shape* of the work: how I research an
edge, how I turn it into a production system, and what operating it live has
taught me.

**A deeper walkthrough of the private production repo is available on request.**

---

## What I run

A single Python monorepo, deployed via Docker Compose, running several
independent live strategy services against real venues:

| Strategy surface | Venues | Core ideas |
|---|---|---|
| Cross-venue funding-rate arbitrage | Lighter, Hyperliquid (USDC perps) | Delta-neutral two-leg book capturing funding differentials; venue-agnostic leg abstraction so any venue pair can be slotted in |
| Crypto market making | Nado, Hyperliquid | Symmetric quoting with online microstructure models: fill-hazard intensity, adverse-selection markout, Hawkes trade-arrival telemetry; EV-gated quoting; optional cross-exchange inventory hedging that fails closed |
| Short-dated binary markets | Polymarket (5-minute BTC up/down) | Brownian-motion probability model recalibrated online (Platt scaling on realized outcomes), maker-limit EV gating, fail-closed arming gate on the official Chainlink resolution oracle |
| Options relative value | Deribit, Derive.xyz | Continuous chain snapshotting, cross-venue vol-surface spread monitoring and execution |
| Multi-layer CFD book | FX/commodity CFDs | Tactical / macro / relative-value layers sharing a calendar-aware research stack |

Everything below the strategy layer is shared infrastructure.

## Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │               Strategy layer                │
                        │ funding arb · market making · binary 5-min  │
                        │ options RV · CFD multi-style book           │
                        └───────────────┬─────────────────────────────┘
   market data                          ▼
 ┌──────────────┐   signals   ┌──────────────────┐  target Δ  ┌────────────────┐
 │ WS feeds     ├────────────►│ Execution engine ├───────────►│ RiskController │
 │ REST polling │             │ (signal → target │            │ exposure caps  │
 │ chain        │             │  position, EV    │            │ daily-loss     │
 │ snapshots    │             │  gates)          │            │ breaker, kill  │
 │ oracles      │             └──────────────────┘            │ switch         │
 └──────────────┘                                             └───────┬────────┘
                                                                      ▼
 ┌──────────────────┐   fills    ┌───────────────┐  orders  ┌──────────────────┐
 │ PositionManager  │◄───────────┤  Venue adapters│◄────────┤  OrderManager    │
 │ + reconciliation │            │  (per-exchange │         │  market / limit /│
 │ (see src/)       │            │   REST + WS)   │         │  chase / post-   │
 └────────┬─────────┘            └───────────────┘          │  only w/ clamp   │
          ▼                                                  └──────────────────┘
 ┌──────────────────────────────┐
 │ Storage (Postgres / SQLite)  │──► Streamlit dashboards, live monitor
 │ runs · trades · equity ·     │
 │ signal quality               │
 └──────────────────────────────┘
```

Design choices that have mattered in practice:

- **Venue adapters behind one interface.** New venues (Lighter's async SDK,
  Deribit, Polymarket's CLOB, Derive) slot in without touching strategy code.
  A `PaperExchange` implements the same interface with simulated fills,
  leverage, funding and liquidation, so every strategy runs paper and live
  from the same code path.
- **Fail-closed everywhere.** Live arming requires the official resolution
  oracle to be healthy; an unhealthy cross-exchange hedge cancels the primary
  maker's quotes; unknown balance reads as *unknown*, never as zero.
- **Graceful shutdown is part of the strategy.** Services flatten positions on
  SIGTERM so `docker stop` is a safe operation, not an incident.
- **Online models ship in shadow mode first**, logging what they *would* have
  done before they are allowed to move spreads or sizes.

## Lessons from operating live (the short version)

These cost real money to learn and shaped the engineering above:

1. **Post-only orders that would cross get rejected — or worse, silently
   converted.** Fixed by clamping maker prices against the live book before
   submission, and converting must-cross orders to explicitly capped IOC.
2. **Cancel/fill races create untracked fills.** A TTL cancel and a fill can
   pass each other on the wire; if you only track fills on orders you think
   are open, your position drifts from the venue's truth. The reconciliation
   module in `src/` is a generic version of the fix: every venue fill is
   classified as tracked, post-cancel, or unknown, and position is
   periodically reconciled against the venue rather than assumed.
3. **EV gates need an exploration floor.** A fill-hazard model that learns
   "I never get filled" stops quoting, which stops generating the
   observations that could teach it otherwise — a self-confirming deadlock.
4. **Partial fills break symmetric books.** A two-leg arb that caps one leg
   (exchange cap, nonce contention) leaves net exposure; sizing and retry
   logic must treat the *pair* as the unit, not the order.
5. **Risk limits must be unit-consistent.** A portfolio exposure cap defined
   pre-leverage while sizing multiplies by leverage is two correct systems
   composing into a wrong one.

## What's in this repo

| Path | What it shows |
|---|---|
| [`notebooks/01_funding_rate_arbitrage.ipynb`](notebooks/01_funding_rate_arbitrage.ipynb) | Research workflow: pull public funding histories from Binance, Bybit and OKX, quantify cross-venue funding spreads, model fees and execution costs, and backtest a simple delta-neutral rotation — with honest treatment of capacity, legging risk and venue risk |
| [`notebooks/02_short_horizon_probability_calibration.ipynb`](notebooks/02_short_horizon_probability_calibration.ipynb) | Why raw model probabilities lose money as a maker: a Brownian-bridge probability model for 5-minute BTC direction on public 1-minute data, reliability diagrams, Platt recalibration implemented from scratch, and an expected-value gate for maker quoting in a binary market |
| [`src/showcase/reconciliation.py`](src/showcase/reconciliation.py) | Production-style code: a generic order/fill reconciler that detects post-cancel fill races, unknown fills and position drift (lesson 2 above), with unit tests |

The notebooks fetch live public data and fall back to the small cached CSVs
committed under `data/`, so the committed outputs are reproducible offline.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                      # unit tests for src/
jupyter lab notebooks/      # or just read the committed outputs on GitHub
```

---

*Nothing in this repository is trading advice. All strategy parameters here
are illustrative, not the ones I trade.*
