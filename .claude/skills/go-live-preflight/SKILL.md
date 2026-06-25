---
name: go-live-preflight
description: Run before switching the bot from paper to live trading on Polymarket. A deliberate, human-invoked safety gate that verifies secrets, funding, SDK currency, risk limits, and a paper dry-run before any real money is committed. Use only when the user explicitly types /go-live-preflight.
disable-model-invocation: true
allowed-tools: Bash Read Grep
---

# Go-Live Preflight

This is a GATE, not a launcher. Do not flip `PAPER_TRADE` yourself. Walk every
step below in order. If ANY step fails or is uncertain, STOP and report — do not
proceed to the next step. Only after all steps pass do you tell the user the
exact manual command to go live.

## Step 1 — Confirm intent
Ask the user to confirm, in this turn: bankroll is $100, they accept that these
15-min markets are efficiency-hunted, and they want to commit REAL money.
If they hesitate, stop here and stay in paper mode.

## Step 2 — Secrets present and sane (never print values)
Check `.env` exists and contains non-empty `POLYMARKET_PRIVATE_KEY`,
`POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`. Confirm `.env` is gitignored.
Report only PRESENT/MISSING per key — never echo a secret.
Sanity: if signature_type is 1 or 2, remind that FUNDER is the proxy/deposit
address (different from the signer), not the EOA.

## Step 3 — Funding and allowances
Confirm with the user that the funder address holds USDC on Polygon (chain 137),
and — if signature_type is 0 (EOA) — that USDC/CTF allowances were set once.
The bot does not fund the wallet; this is on the user.

## Step 4 — SDK is current
Run `pip show py-clob-client` and report the version. Confirm `exchange.py`
still uses `create_order` + `post_order(..., OrderType.GTC)` and that those
signatures match the installed version's docs. If anything drifted, fix
`exchange.py` first, then restart this preflight.

## Step 5 — Risk limits unchanged
`grep -nE "POSITION_SIZE_USD|MAX_SPREAD|DAILY_STOP_LOSS|MAX_SLIPPAGE" config.py`
Confirm: 2.00 / 0.03 / -15.00 / 0.02. If any differ from spec, stop and ask.

## Step 6 — Code compiles
`python -m py_compile *.py` — must exit clean. Report the result.

## Step 7 — Paper dry-run
With `PAPER_TRADE=True`, run `python main.py` for a few minutes. Confirm: the
scanner finds a BTC 15-min market, the book stream connects, momentum populates,
and at least one signal is evaluated (check the `signals` table). Show a short
summary. If nothing connects, do NOT go live — debug first.

## Step 8 — Go-live instruction (manual, by the user)
Only if Steps 1-7 all passed, tell the user to:
1. Set `PAPER_TRADE = False` in `config.py` themselves.
2. Start the bot and watch the FIRST trade end-to-end before walking away.
3. Keep the `-$15` daily stop in place; do not raise it on day one.

Then remind them the kill path is the `_risk_loop` stop-loss, and Ctrl-C
triggers a graceful shutdown that cancels open orders.
