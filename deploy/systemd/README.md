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
```

Operational notes:

- The console binds only `127.0.0.1:8090`; keep SSH tunnel access.
- `.env.runtime` is loaded by systemd as an EnvironmentFile. Do not print it.
- `ai-quant-trader.service` uses native `sd_notify` readiness and `WatchdogSec`.
- The console stays behind the HTTP readiness watchdog timer because uvicorn is launched directly and does not own the trading worker lifecycle.
- `ai-quant-maintenance.timer` runs SQLite backup, log rotation, backup retention, and disk-space checks.
