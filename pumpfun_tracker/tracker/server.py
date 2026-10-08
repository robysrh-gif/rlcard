"""HTTP API + live dashboard (Server-Sent Events) + the background loops."""
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from .alerts import AlertDispatcher
from .engine import Engine

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "static"


def create_app(engine: Engine, feed, alerts: AlertDispatcher, fetch_sol_price: bool = True) -> web.Application:
    app = web.Application()
    clients: set[asyncio.Queue] = set()

    def broadcast(event: dict) -> None:
        data = json.dumps(event)
        for q in list(clients):
            if q.qsize() < 50:  # drop events for clients that stopped reading
                q.put_nowait(data)

    sending: set[asyncio.Task] = set()

    async def tick_loop():
        while True:
            await asyncio.sleep(engine.config.snapshot_interval_s)
            try:
                new_alerts, pruned = engine.tick()
                if pruned:
                    await feed.unsubscribe(pruned)
                for a in new_alerts:
                    broadcast({"type": "alert", "alert": a})
                    task = asyncio.create_task(alerts.send(a))
                    sending.add(task)
                    task.add_done_callback(sending.discard)
                if clients:
                    broadcast({"type": "snapshot", "stats": engine.stats(), "tokens": engine.ranked(100)})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")

    async def sol_price_loop():
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        async with ClientSession(timeout=ClientTimeout(total=10)) as s:
            while True:
                try:
                    async with s.get(url) as r:
                        engine.sol_usd = float((await r.json())["solana"]["usd"])
                except Exception as e:
                    log.debug("SOL price unavailable: %s", e)
                await asyncio.sleep(60)

    async def on_startup(app):
        app["tasks"] = [asyncio.create_task(feed.run()), asyncio.create_task(tick_loop())]
        if fetch_sol_price:
            app["tasks"].append(asyncio.create_task(sol_price_loop()))

    async def on_cleanup(app):
        tasks = app["tasks"] + list(sending)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await alerts.close()

    # ---- routes ----

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def tokens(request):
        try:
            limit = max(1, min(int(request.query.get("limit", 100)), 500))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be an integer")
        return web.json_response({"stats": engine.stats(),
                                  "tokens": engine.ranked(limit, request.query.get("sort", "score"))})

    async def token_detail(request):
        t = engine.tokens.get(request.match_info["mint"])
        if not t:
            raise web.HTTPNotFound()
        now = engine.clock()
        recent = [{"ts": x.ts, "trader": x.trader, "is_buy": x.is_buy, "sol": round(x.sol, 4)}
                  for x in list(t.trades)[-30:]][::-1]
        return web.json_response({**t.summary(now, engine.sol_usd), "history": t.history(), "recent": recent})

    async def alert_list(_):
        return web.json_response(list(engine.alerts))

    async def stream(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        clients.add(q)
        try:
            await resp.write(f"data: {json.dumps({'type': 'alerts', 'alerts': list(engine.alerts)})}\n\n".encode())
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            clients.discard(q)
        return resp

    async def health(_):
        return web.json_response({"ok": True, **engine.stats()})

    app.router.add_get("/", index)
    app.router.add_get("/api/tokens", tokens)
    app.router.add_get("/api/tokens/{mint}", token_detail)
    app.router.add_get("/api/alerts", alert_list)
    app.router.add_get("/api/stream", stream)
    app.router.add_get("/api/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
