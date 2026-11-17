from sqlalchemy import Column,Integer,String,JSON,DateTime,Text,ForeignKey
from datetime import datetime,timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Node(Base):
    __tablename__='nodes'
    id=Column(Integer,primary_key=True);name=Column(String(80),unique=True,nullable=False);role=Column(String(20),nullable=False);status=Column(String(20),nullable=False)
    lineage=Column(String(80),nullable=False);position=Column(Integer,nullable=False);delay_ms=Column(Integer,nullable=False);note=Column(String(300),default='')
class RequestSet(Base):
    __tablename__='request_sets'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);events=Column(JSON,nullable=False);note=Column(String(300),default='')
class Trial(Base):
    __tablename__='trials'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);request_set_id=Column(Integer,ForeignKey('request_sets.id'),nullable=False)
    max_delay_ms=Column(Integer,nullable=False);sticky_ms=Column(Integer,nullable=False);status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    topology_version_id=Column(Integer,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class TopologyVersion(Base):
    """Immutable, complete node set. Trials bind to one row so they can only ever
    replay the configuration before or after a switch, never a half-applied mix."""
    __tablename__='topology_versions'
    id=Column(Integer,primary_key=True);version=Column(Integer,unique=True,nullable=False);nodes=Column(JSON,nullable=False)
    checksum=Column(String(64),nullable=False);source=Column(String(24),nullable=False,default='change');change_id=Column(Integer,nullable=True)
    created_by=Column(String(120),nullable=False,default='system');created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class TopologyHead(Base):
    """Single row serialising every topology write (drafts publish, quick node edits)."""
    __tablename__='topology_head'
    id=Column(Integer,primary_key=True);head_version_id=Column(Integer,nullable=True)
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class TopologyChange(Base):
    __tablename__='topology_changes'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    status=Column(String(20),nullable=False,default='draft')
    base_version_id=Column(Integer,nullable=True);base_nodes=Column(JSON,nullable=False);draft_nodes=Column(JSON,nullable=False)
    conflicts=Column(JSON,nullable=False);validation_errors=Column(JSON,nullable=False);resolution_history=Column(JSON,nullable=False)
    apply_plan=Column(JSON,nullable=True);attempts=Column(Integer,nullable=False,default=0);revision=Column(Integer,nullable=False,default=1)
    error=Column(Text,nullable=True);applied_version_id=Column(Integer,nullable=True)
    created_by=Column(String(120),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));applied_at=Column(DateTime(timezone=True),nullable=True)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
