"""FastAPI dashboard backend for the Bot Arena."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import config
import db
import learning

app = FastAPI(title="Polymarket Bot Arena Dashboard")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text()


@app.get("/api/status")
async def get_status():
    return {
        "mode": config.get_current_mode(),
        "venue": config.get_venue(),
        "max_position": config.get_max_position(),
        "max_daily_loss_per_bot": config.get_max_daily_loss_per_bot(),
        "max_daily_loss_total": config.get_max_daily_loss_total(),
    }


@app.post("/api/mode")
async def set_mode(request: Request):
    body = await request.json()
    mode = body.get("mode")
    if mode not in ("paper", "live"):
        return JSONResponse({"error": "Mode must be 'paper' or 'live'"}, 400)
    config.set_trading_mode(mode)
    return {"mode": config.get_current_mode()}


@app.get("/api/markets")
async def get_markets():
    """Get active BTC 5-min markets with close times."""
    import requests as req
    try:
        api_key = json.load(open(config.SIMMER_API_KEY_PATH))["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = req.get(
            f"{config.SIMMER_BASE_URL}/api/sdk/markets",
            headers=headers,
            params={"status": "active", "limit": 50},
            timeout=10,
        )
        data = resp.json()
        markets_list = data if isinstance(data, list) else data.get("markets", [])
        btc_markets = []
        for m in markets_list:
            q = m.get("question", "").lower()
            if "bitcoin" in q and "up or down" in q:
                btc_markets.append({
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "current_price": m.get("current_price"),
                    "resolves_at": m.get("resolves_at"),
                    "url": m.get("url"),
                })
        return JSONResponse(btc_markets)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/overview")
async def get_overview():
    stats = db.get_dashboard_stats()
    active_bots = db.get_active_bots()
    return JSONResponse({
        "stats": stats,
        "active_bots": active_bots,
        "mode": config.get_current_mode(),
    })


@app.get("/api/bots")
async def get_bots():
    active = db.get_active_bots()
    result = []
    for bot_cfg in active:
        # Parse params JSON string if needed
        cfg = dict(bot_cfg)
        if isinstance(cfg.get("params"), str):
            try:
                cfg["params"] = json.loads(cfg["params"])
            except (json.JSONDecodeError, TypeError):
                pass
        perf_12h = db.get_bot_performance(cfg["bot_name"], hours=12)
        perf_24h = db.get_bot_performance(cfg["bot_name"], hours=24)
        trades = db.get_bot_trades(cfg["bot_name"], limit=10)
        # Count pending (unresolved) trades so dashboard shows activity
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM trades WHERE bot_name=? AND outcome IS NULL",
                (cfg["bot_name"],)
            ).fetchone()
            pending_count = dict(row)["c"]
        result.append({
            "config": cfg,
            "performance_12h": perf_12h,
            "performance_24h": perf_24h,
            "recent_trades": trades,
            "pending_trades": pending_count,
        })
    return JSONResponse(result)


@app.get("/api/evolution")
async def get_evolution():
    history = db.get_evolution_history(limit=20)
    for h in history:
        for key in ("survivors", "replaced", "new_bots", "rankings"):
            if isinstance(h.get(key), str):
                h[key] = json.loads(h[key])
    return JSONResponse(history)


@app.get("/api/trades")
async def get_trades(bot: str = None, limit: int = 50):
    if bot:
        return JSONResponse(db.get_bot_trades(bot, limit=limit))
    with db.get_conn() as conn:
        # Show trades with real P&L first, then pending. Skip phantom pnl=0 resolved trades.
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE NOT (outcome IS NOT NULL AND (pnl IS NULL OR pnl = 0))
               ORDER BY
                   CASE WHEN outcome IS NOT NULL THEN 0 ELSE 1 END,
                   resolved_at DESC, created_at DESC
               LIMIT ?""", (limit,)
        ).fetchall()
        return JSONResponse([dict(r) for r in rows])


@app.get("/api/copytrading")
async def get_copytrading():
    from copytrading.copier import TradeCopier
    from copytrading.tracker import WalletTracker
    tracker = WalletTracker()
    copier = TradeCopier(tracker)
    return JSONResponse({
        "wallets": tracker.get_tracked(),
        "stats": copier.get_copy_stats(),
    })


@app.get("/api/earnings")
async def get_earnings():
    with db.get_conn() as conn:
        daily = conn.execute("""
            SELECT date(created_at) as day, COALESCE(SUM(pnl), 0) as pnl,
                   COUNT(*) as trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE outcome IN ('win', 'loss')
            GROUP BY date(created_at) ORDER BY day DESC LIMIT 30
        """).fetchall()

        best = conn.execute(
            "SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY pnl DESC LIMIT 5"
        ).fetchall()

        worst = conn.execute(
            "SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY pnl ASC LIMIT 5"
        ).fetchall()

        return JSONResponse({
            "daily": [dict(r) for r in daily],
            "best_trades": [dict(r) for r in best],
            "worst_trades": [dict(r) for r in worst],
        })


@app.get("/api/learning")
async def get_learning():
    active = db.get_active_bots()
    result = {}
    for bot_cfg in active:
        name = bot_cfg["bot_name"]
        result[name] = learning.get_bot_learning_summary(name)
    return JSONResponse(result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)
