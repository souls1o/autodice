# Wallet smoke checks (LTC / ETH / SOL + USDT/USDC)

Prereqs: `HOUSE_MNEMONIC`, `ETH_RPC_URL`, `BNB_RPC_URL`, `SOL_RPC_URL`, `LTC_API_URL` (+ optional `LTC_API_TOKEN`) in `.env`. Mongo running (spend registry).

## 1. Address stability (per ticket)

1. Open a ticket channel; run `!ltc`, `!eth`, `!sol` and note addresses.
2. Restart the bot; run the same commands again in that channel.
3. Expect: identical addresses each time. Players send USDT/USDC to the same `!eth` (ETH or BNB) or `!sol` address — no separate stablecoin commands.

## 2. Inbound credit → self hold

1. On a ticket with self hold > $0, post `!eth` or `!sol`.
2. Send native **or** USDT/USDC on that chain to the posted address.
3. Expect: ticket notice `📥 Received $…`, self hold decreases once; asset is registered for later outbounds.

## 3. Multi-source outbound send

Fund **two or more** registered addresses for the same coin, then admin `!withdraw <coin> <addr> <usd>` (e.g. `usdt@eth`, `usdc@bnb`, `usdt@sol`).

| Coin | Expect |
|------|--------|
| **LTC** | One broadcast tx spending multiple UTXOs / addresses. |
| **SOL** (native) | One tx with multiple transfer ixs. |
| **ETH** (native) | Consolidate logs if split, then one payment tx. |
| **USDT/USDC @ eth/bnb** | Token consolidate-then-send; gas paid in native ETH/BNB. |
| **USDT/USDC @ sol** | One SPL transfer tx; fee payer needs SOL. |

## 4. House balance

`!housebal` / `!hb` should include native + stable registered balances (stables at $1).
