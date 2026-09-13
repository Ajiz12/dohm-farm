# DOHM Testnet Farming Bot

24/7 automated farming for [DOHM testnet](https://testnet.dohm.finance). Stake → Unstake → Claim bonds → Repeat.

## Features

- 🔄 **24/7 Auto-farming** — infinite loop with configurable cycle limits
- 🤖 **Supervisor** — auto-restart on crash with exponential backoff
- 📱 **Telegram Notifications** — start, goal, crash, heartbeat alerts
- 🔄 **Auto Update** — fetch new version from URL, SHA256 compare, hot-replace
- 🔒 **Lock File** — prevent duplicate instances
- 🎲 **Random Jitter** — wait times ±60s to avoid bot detection
- 🛡️ **Retry + Backoff** — 3 attempts per action with exponential delay
- 📊 **Heartbeat** — status file + optional Telegram heartbeat every N cycles

## Quick Start

```bash
# 1. Install dependencies
pip install camoufox

# 2. Copy env template
cp .env.example .env
# Edit .env with your credentials

# 3. Run
source .env
python3 dohm_farm.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_PASSWORD` | *required* | Wallet password |
| `SEED_PHRASE` | *required* | 12-word recovery phrase |
| `POINTS_TARGET` | `1000` | Stop when points reach this |
| `MAX_CYCLES` | `0` | 0 = infinite 24/7 |
| `WAIT_MINUTES` | `10` | Minutes to wait between actions |
| `WAIT_JITTER` | `60` | Random ± seconds added to wait |
| `STAKE_AMOUNT` | `0.2` | DOHM to stake per cycle |
| `UNSTAKE_AMOUNT` | `0.1` | sDOHM to unstake per cycle |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | `""` | Telegram chat ID |
| `NOTIFY_ON_HEARTBEAT` | `0` | Send heartbeat every N cycles (0=off) |
| `AUTO_UPDATE` | `0` | Enable auto-update (1=on) |
| `UPDATE_URL` | `""` | URL to check for new version |
| `UPDATE_CHECK_EVERY_CYCLE` | `5` | Check for update every N cycles |

## Farming Flow

```
Stake X DOHM → Wait 10m → Unstake Y sDOHM → Wait 10m → Claim Bonds → Wait 10m → Repeat
```

## Scripts

| Script | Description |
|--------|-------------|
| `dohm_farm.py` | Main farming bot (Camoufox, 24/7) |
| `claim_all_sdohm.py` | Batch claim sDOHM unstakes (Chromium) |

## Running 24/7

```bash
# Using screen
screen -S dohm
source .env && python3 dohm_farm.py
# Ctrl+A, D to detach

# Re-attach
screen -r dohm
```

## Logs

- **Console**: real-time output
- **File**: `/tmp/dohm_farm.log`
- **Heartbeat**: `/tmp/dohm_farm.heartbeat`
- **Lock**: `/tmp/dohm_farm.lock`

## License

MIT
