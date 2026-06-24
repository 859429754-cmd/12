# AI Quant systemd deployment

These units are templates for `/root/ai-quant-trader`.

Install manually on the server:

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-quant-console.service
sudo systemctl enable --now ai-quant-trader.service
sudo systemctl enable --now ai-quant-health-watchdog.timer
sudo systemctl enable --now ai-quant-maintenance.timer
sudo systemctl enable --now ai-quant-alerts.timer
```

Operational notes:

- The console binds only `127.0.0.1:8090`; keep SSH tunnel access.
- `.env.runtime` is loaded by systemd as an EnvironmentFile. Do not print it.
- `ai-quant-trader.service` uses native `sd_notify` readiness and `WatchdogSec`.
- Default low-memory deployments should run one consolidated worker through `ai-quant-trader.service`; it owns trading, news refresh, price wakeup, and order-status polling in a single Python process.
- `ai-quant-order-status-worker.service` is retained only for decoupled-worker deployments. Do not enable it together with `ai-quant-trader.service` unless you intentionally want duplicate order-status polling.
- The console stays behind an HTTP health watchdog timer because uvicorn is launched directly and readiness is protected by account login.
- `ai-quant-maintenance.timer` runs SQLite backup, log rotation, backup retention, and disk-space checks.
- `ai-quant-alerts.timer` polls runtime alerts every minute and forwards them when `AI_QUANT_ALERT_WEBHOOK_URL` is configured.
