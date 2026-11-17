import os,sys,tempfile,pathlib,importlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
_tmp=tempfile.mkdtemp(prefix='replica-route-tests-')
os.environ.setdefault('DATABASE_URL','sqlite+pysqlite:///'+os.path.join(_tmp,'test.db'))
os.environ.setdefault('REDIS_URL','redis://localhost:6379/15')
os.environ.setdefault('SEED_DEMO','false')
import pytest
from fastapi.testclient import TestClient
from src.config.database import Base,engine,Session
from src.models import entities
from src.services import topology
import src.services.trials as trial_service
from src.main import app


class FakeQueue:
    def __init__(self):self.items=[]
    def rpush(self,key,value):self.items.append(value)
    def blpop(self,*a,**k):return None
    def ping(self):return True


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    trial_service.queue=FakeQueue()
    with Session() as db:
        from src.models.entities import User,RequestSet,Node
        from src.utils.security import password_hash
        for role in ('admin','operator','viewer'):
            db.add(User(email=role+'@qiyuan.test',password=password_hash('Demo2026!'),role=role))
        for name,role,pos,delay in [('harbor-primary','primary',105,0),('north-replica','replica',104,150),('south-replica','replica',105,20)]:
            db.add(Node(name=name,role=role,status='healthy',lineage='qiyuan-log-01',position=pos,delay_ms=delay,note=''))
        db.add(RequestSet(code='QY-1',title='写后读',events=[{'at_ms':0,'kind':'write','session':'a'},{'at_ms':100,'kind':'read','session':'a'}],note=''))
        db.commit()
    topology.startup()
    yield


@pytest.fixture
def client():return TestClient(app)


def auth(client,role='operator'):
    res=client.post('/auth/login',json={'email':role+'@qiyuan.test','password':'Demo2026!'})
    assert res.status_code==200,res.text
    return {'Authorization':'Bearer '+res.json()['token']}


def current_nodes(client,h=None):
    return client.get('/topology',headers=h or auth(client)).json()['head']['nodes']


def draft(client,headers,name='切换主节点',nodes=None):
    nodes=nodes if nodes is not None else current_nodes(client)
    res=client.post('/topology/changes',json={'name':name,'nodes':nodes},headers=headers)
    assert res.status_code==201,res.text
    return res.json()


def edited(nodes,predicate,changes):
    out=[]
    for node in nodes:
        if predicate(node):
            out.append({**node,**changes})
        else:
            out.append(node)
    return out
