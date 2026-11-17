from sqlalchemy import select
from redis import Redis
from fastapi import HTTPException
from src.models.entities import RequestSet,Trial
from src.config.settings import REDIS_URL
from src.constants.enums import RUN_STATES
from src.utils.serialization import record
from .common import save
from .routing import simulate
from . import topology
queue=Redis.from_url(REDIS_URL,decode_responses=True)
def listing(db):return [record(x) for x in db.scalars(select(Trial).order_by(Trial.id.desc()))]
def detail(db,ident):
    obj=db.get(Trial,ident)
    if not obj:raise HTTPException(404,'试验不存在')
    return record(obj)
def create(db,data,user):
    req=db.get(RequestSet,data.request_set_id)
    if not req:raise HTTPException(404,'请求集不存在')
    version=data.topology_version_id and topology.get_version(db,data.topology_version_id)
    if data.topology_version_id and not version:raise HTTPException(404,'指定的拓扑版本不存在')
    if not version:version=topology.current(db)
    if not version:raise HTTPException(422,'尚无生效拓扑，请先发布拓扑变更')
    nodes=version.nodes
    try:simulate(nodes,req.events,data.max_delay_ms,data.sticky_ms)
    except ValueError as e:raise HTTPException(422,str(e))
    snapshot={'nodes':nodes,'request_set':record(req),'topology_version':{'id':version.id,'version':version.version,'checksum':version.checksum}}
    obj=Trial(**data.model_dump(exclude={'topology_version_id'}),input_snapshot=snapshot,status=RUN_STATES[0],topology_version_id=version.id)
    result=save(db,obj,user,'路由演练入队 '+data.name+'（拓扑 v%d）'%version.version)
    try:queue.rpush('replicaroute:queue',str(obj.id))
    except Exception:
        obj.status='failed';obj.error='队列暂时不可用，请重新创建';db.commit();raise HTTPException(503,obj.error)
    return result
