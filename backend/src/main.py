import os,logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from src.config.database import Base,engine
from src.config.migrations import ensure_schema
from src.models import entities
from src.controllers.auth import router as auth
from src.routes import nodes,request_sets,trials,audit,topology
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.request_id import RequestIdMiddleware
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    try:
        ensure_schema()
    except Exception: logging.exception('schema upgrade failed; dependent features may be unavailable')
    if os.getenv('SEED_DEMO','true')=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception: logging.exception('seed failed; manual retry is available')
    try:
        from src.services.topology import startup as topology_startup
        topology_startup()
    except Exception: logging.exception('topology bootstrap/recovery failed; /topology/recover can retry')
    yield
app=FastAPI(title='站点读流量路由演练',lifespan=lifespan)
app.add_middleware(ErrorMiddleware);app.add_middleware(RequestIdMiddleware)
for r in (auth,nodes.router,request_sets.router,trials.router,audit.router,topology.router): app.include_router(r)
@app.get('/health')
def health():
    with engine.connect() as c: c.execute(text('SELECT 1'))
    from src.services.trials import queue
    queue.ping()
    return {'status':'healthy'}
