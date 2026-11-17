from conftest import auth,current_nodes,draft,edited
from src.config.database import Session
from src.models.entities import TopologyChange,TopologyVersion,Trial
from src.services import topology


def test_primary_switch_is_one_atomic_version(client):
    h=auth(client,'admin')
    before=current_nodes(client,h)
    assert len([n for n in before if n['role']=='primary'])==1
    target=edited(before,lambda n:n['name']=='south-replica',{'role':'primary'})
    target=edited(target,lambda n:n['name']=='harbor-primary',{'role':'replica'})
    change=draft(client,h,'南站升主',target)
    res=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-switch-0001'},headers=h)
    assert res.status_code==200,res.text
    body=res.json()
    assert body['status']=='applied'
    assert body['applied_version']['version']==body['base_version']['version']+1
    after=current_nodes(client,h)
    assert [n['name'] for n in after if n['role']=='primary']==['south-replica']
    # No reader ever saw an intermediate version: published versions are wholes.
    versions=client.get('/topology/versions',headers=h).json()
    assert versions[0]['node_count']==len(after)
    for version in versions:
        detail=client.get('/topology/versions/%d'%version['id'],headers=h).json()
        assert len([n for n in detail['nodes'] if n['role']=='primary'])==1


def test_draft_cannot_overwrite_intervening_edit_and_conflict_keeps_both_sides(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'调整南站',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':10}))
    # Intervening quick edit on the same node.
    node=next(n for n in base if n['name']=='south-replica')
    assert client.put('/nodes/%d'%node['id'],json={**{k:node[k] for k in ('name','role','status','lineage','position','delay_ms','note')},'delay_ms':99},headers=h).status_code==200
    check=client.post('/topology/changes/%d/validate'%change['id'],headers=h).json()
    assert check['stale_base'] is True
    assert [c['node'] for c in check['conflicts']]==['south-replica']
    conflict=check['conflicts'][0]
    assert conflict['draft']['delay_ms']==10 and conflict['current']['delay_ms']==99 and conflict['base']['delay_ms']==20
    blocked=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-conflict-01'},headers=h)
    assert blocked.status_code==409
    # Unresolved conflict keeps the live value untouched.
    assert next(n for n in current_nodes(client,h) if n['name']=='south-replica')['delay_ms']==99
    resolved=client.post('/topology/changes/%d/resolve'%change['id'],json={'resolutions':[{'node':'south-replica','choice':'draft','note':'以草稿延迟为准'}]},headers=h)
    assert resolved.status_code==200,resolved.text
    assert resolved.json()['unresolved_count']==0
    assert [h0['choice'] for h0 in resolved.json()['resolution_history']]==['draft']
    ok=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-conflict-02','confirm_stale_base':True},headers=h)
    assert ok.status_code==200,ok.text
    assert next(n for n in current_nodes(client,h) if n['name']=='south-replica')['delay_ms']==10


def test_stale_draft_requires_explicit_rebase(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'旧草稿',edited(base,lambda n:n['name']=='north-replica',{'note':'调整'}))
    primary=next(n for n in base if n['name']=='harbor-primary')
    client.put('/nodes/%d'%primary['id'],json={**{k:primary[k] for k in ('name','role','status','lineage','position','delay_ms','note')},'note':'期间修改'},headers=h)
    res=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-stale-001'},headers=h)
    assert res.status_code==409
    assert '前进' in res.json()['detail']
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-stale-002','confirm_stale_base':True},headers=h).status_code==200
    names={n['name']:n for n in current_nodes(client,h)}
    assert names['harbor-primary']['note']=='期间修改'  # intervening edit preserved
    assert names['north-replica']['note']=='调整'


def test_only_one_concurrent_publish_wins(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    first=draft(client,h,'方案A',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':5}))
    second=draft(client,h,'方案B',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':6}))
    assert client.post('/topology/changes/%d/publish'%first['id'],json={'request_id':'req-race-0001'},headers=h).status_code==200
    loser=client.post('/topology/changes/%d/publish'%second['id'],json={'request_id':'req-race-0002'},headers=h)
    assert loser.status_code==409
    with Session() as db:
        assert len(db.query(TopologyVersion).all())==2  # bootstrap + one winner
        assert db.get(TopologyChange,second['id']).status=='draft'


def test_same_request_id_retry_never_applies_twice(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'重试幂等',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':7}))
    first=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-idem-0001'},headers=h)
    assert first.status_code==200,first.text
    again=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-idem-0001'},headers=h)
    assert again.status_code==200
    with Session() as db:
        assert db.scalar(__import__('sqlalchemy').select(__import__('sqlalchemy').func.count()).select_from(TopologyVersion))==2
        attempts=db.get(TopologyChange,change['id']).apply_plan['attempts']
        assert len([a for a in attempts if a['outcome']=='applied'])==1


def test_interrupted_switch_recovers_same_record(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'中断恢复',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':8}))
    with Session() as db:
        obj=db.get(TopologyChange,change['id'])
        obj.status='applying'
        obj.apply_plan={'attempts':[{'request_id':'req-crash-01','outcome':'running','actor':'admin@qiyuan.test'}]}
        db.commit()
    with Session() as db:
        topology.recover(db)
    detail=client.get('/topology/changes/%d'%change['id'],headers=h).json()
    assert detail['status']=='draft'
    assert '中断' in detail['error']
    retry=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-crash-02'},headers=h)
    assert retry.status_code==200,retry.text
    with Session() as db:
        assert db.query(TopologyChange).count()==1  # same record, no duplicate
        assert db.query(TopologyVersion).count()==2


def test_recovery_finishes_switch_that_had_committed(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'已提交未回写',edited(base,lambda n:n['name']=='north-replica',{'note':'x'}))
    with Session() as db:
        obj=db.get(TopologyChange,change['id'])
        version=topology._write_version(db,[topology.norm(n) for n in obj.draft_nodes],'change',obj.id,'admin@qiyuan.test')
        obj.status='applying'
        db.commit();vid=version.id
    with Session() as db:topology.recover(db)
    detail=client.get('/topology/changes/%d'%change['id'],headers=h).json()
    assert detail['status']=='applied'
    assert detail['applied_version_id']==vid
    with Session() as db:assert db.query(TopologyVersion).count()==2


def test_trials_freeze_version_and_keep_original_snapshot(client):
    h=auth(client,'admin')
    trial=client.post('/trials',json={'name':'切换前','request_set_id':1,'max_delay_ms':100,'sticky_ms':500},headers=h)
    assert trial.status_code==201,trial.text
    old=trial.json()
    base=current_nodes(client,h)
    change=draft(client,h,'换主',edited(edited(base,lambda n:n['name']=='south-replica',{'role':'primary'}),lambda n:n['name']=='harbor-primary',{'role':'replica'}))
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-trial-001'},headers=h).status_code==200
    detail=client.get('/trials/%d'%old['id'],headers=h).json()
    assert detail['input_snapshot']==old['input_snapshot']
    assert [n['name'] for n in detail['input_snapshot']['nodes'] if n['role']=='primary']==['harbor-primary']
    old_version=old['topology_version_id']
    newer=client.post('/trials',json={'name':'切换后','request_set_id':1,'topology_version_id':None},headers=h)
    assert newer.status_code==201
    assert newer.json()['topology_version_id']!=old_version
    snapshot=newer.json()['input_snapshot']
    assert [n['name'] for n in snapshot['nodes'] if n['role']=='primary']==['south-replica']


def test_invalid_drafts_are_rejected_on_whole_set(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    two_primaries=edited(base,lambda n:n['name']=='north-replica',{'role':'primary'})
    change=draft(client,h,'双主',two_primaries)
    check=client.post('/topology/changes/%d/validate'%change['id'],headers=h).json()
    assert check['publishable'] is False
    assert any('只能配置一个主节点' in e for e in check['validation_errors'])
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-invalid-1'},headers=h).status_code==422
    no_primary=edited(base,lambda n:n['name']=='harbor-primary',{'role':'replica'})
    other=draft(client,h,'无主',no_primary)
    assert client.post('/topology/changes/%d/publish'%other['id'],json={'request_id':'req-invalid-2'},headers=h).status_code==422
    assert len([n for n in current_nodes(client,h) if n['role']=='primary'])==1


def test_lineage_and_position_checks(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    bad=edited(base,lambda n:n['name']=='south-replica',{'position':200})
    change=draft(client,h,'越界',bad)
    detail=client.post('/topology/changes/%d/validate'%change['id'],headers=h).json()
    assert any('位点不得高于' in e for e in detail['validation_errors'])
    bad_lineage=edited(base,lambda n:n['name']=='south-replica',{'lineage':'other-lineage'})
    change2=draft(client,h,'异谱系',bad_lineage)
    assert client.post('/topology/changes/%d/publish'%change2['id'],json={'request_id':'req-lineage-1'},headers=h).status_code==200
    # Divergent lineage is allowed by design; replicas simply stop qualifying.
    assert next(n for n in current_nodes(client,h) if n['name']=='south-replica')['lineage']=='other-lineage'


def test_viewer_cannot_publish(client):
    operator=auth(client,'operator')
    base=current_nodes(client,operator)
    change=draft(client,operator,'观察员不可发布',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':9}))
    viewer=auth(client,'viewer')
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-viewer-01'},headers=viewer).status_code==403
    assert client.put('/topology/changes/%d'%change['id'],json={'nodes':base},headers=viewer).status_code==403


def test_quick_node_edit_cannot_create_second_primary(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    node=next(n for n in base if n['name']=='north-replica')
    res=client.put('/nodes/%d'%node['id'],json={**{k:node[k] for k in ('name','role','status','lineage','position','delay_ms','note')},'role':'primary'},headers=h)
    assert res.status_code==422
    assert len([n for n in current_nodes(client,h) if n['role']=='primary'])==1


def test_trial_during_pending_draft_uses_live_version(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    draft(client,h,'草稿进行中',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':1}))
    # A draft is not live: trials still freeze the current head and do not see it.
    res=client.post('/trials',json={'name':'草稿期间','request_set_id':1},headers=h)
    assert res.status_code==201,res.text
    snapshot=res.json()['input_snapshot']
    assert next(n for n in snapshot['nodes'] if n['name']=='south-replica')['delay_ms']==20
    head=client.get('/topology',headers=h).json()['head']
    assert snapshot['topology_version']['id']==head['id']


def test_trial_can_pin_previous_version_after_switch(client):
    h=auth(client,'admin')
    old_head=client.get('/topology',headers=h).json()['head']
    first=client.post('/trials',json={'name':'旧版本试验','request_set_id':1,'topology_version_id':old_head['id']},headers=h)
    assert first.status_code==201,first.text
    base=current_nodes(client,h)
    change=draft(client,h,'换主后',edited(edited(base,lambda n:n['name']=='south-replica',{'role':'primary'}),lambda n:n['name']=='harbor-primary',{'role':'replica'}))
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-ver-0001'},headers=h).status_code==200
    pinned=client.post('/trials',json={'name':'重放旧版本','request_set_id':1,'topology_version_id':old_head['id']},headers=h)
    assert pinned.status_code==201,pinned.text
    assert [n['name'] for n in pinned.json()['input_snapshot']['nodes'] if n['role']=='primary']==['harbor-primary']
    missing=client.post('/trials',json={'name':'不存在版本','request_set_id':1,'topology_version_id':99999},headers=h)
    assert missing.status_code==404


def test_applying_change_blocks_competing_publish(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    first=draft(client,h,'方案A',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':11}))
    second=draft(client,h,'方案B',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':12}))
    with Session() as db:
        obj=db.get(TopologyChange,first['id'])
        obj.status='applying'
        obj.apply_plan={'head_at_start':client.get('/topology',headers=h).json()['head']['id'],
                        'attempts':[{'request_id':'req-mid-01','outcome':'running'}]}
        db.commit()
    # The other plan may still be edited/validated but cannot win while A is mid-switch.
    check=client.post('/topology/changes/%d/validate'%second['id'],headers=h)
    assert check.status_code==200
    blocked=client.post('/topology/changes/%d/publish'%second['id'],json={'request_id':'req-mid-02'},headers=h)
    assert blocked.status_code==409
    with Session() as db:
        assert db.query(TopologyVersion).count()==1


def test_concurrent_publish_threads_only_one_winner(client):
    import threading
    h=auth(client,'admin')
    base=current_nodes(client,h)
    plans=[draft(client,h,'并发方案%d'%i,edited(base,lambda n:n['name']=='south-replica',{'delay_ms':30+i})) for i in range(3)]
    barrier=threading.Barrier(3)
    results=[]
    def fire(index):
        barrier.wait()
        res=client.post('/topology/changes/%d/publish'%plans[index]['id'],json={'request_id':'req-thread-%d'%index},headers=h)
        results.append((index,res.status_code,res.json().get('status') or res.json().get('detail')))
    threads=[threading.Thread(target=fire,args=(i,)) for i in range(3)]
    [t.start() for t in threads];[t.join() for t in threads]
    winners=[r for r in results if r[1]==200]
    assert len(winners)==1,results
    with Session() as db:
        assert db.query(TopologyVersion).count()==2
        assert db.query(TopologyChange).filter(TopologyChange.status=='applied').count()==1
    head=current_nodes(client,h)
    assert len([n for n in head if n['role']=='primary'])==1


def test_intervening_quick_edit_during_draft_is_not_lost(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'只改南站延迟',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':33}))
    north=next(n for n in base if n['name']=='north-replica')
    client.put('/nodes/%d'%north['id'],json={**{k:north[k] for k in ('name','role','status','lineage','position','delay_ms','note')},'note':'独立修改'},headers=h)
    check=client.post('/topology/changes/%d/validate'%change['id'],headers=h).json()
    assert check['conflicts']==[]
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-merge-001','confirm_stale_base':True},headers=h).status_code==200
    nodes={n['name']:n for n in current_nodes(client,h)}
    assert nodes['north-replica']['note']=='独立修改'
    assert nodes['south-replica']['delay_ms']==33


def test_retry_after_interruption_with_same_request_id_applies_once(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'中断后同ID重试',edited(base,lambda n:n['name']=='south-replica',{'delay_ms':21}))
    with Session() as db:
        obj=db.get(TopologyChange,change['id'])
        obj.status='applying'
        obj.apply_plan={'attempts':[{'request_id':'req-sameid-01','outcome':'running'}]}
        db.commit()
    with Session() as db:topology.recover(db)
    first=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-sameid-01'},headers=h)
    assert first.status_code==200,first.text
    second=client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-sameid-01'},headers=h)
    assert second.status_code==200
    with Session() as db:
        assert db.query(TopologyVersion).count()==2
        applied=[a for a in db.get(TopologyChange,change['id']).apply_plan['attempts'] if a['outcome']=='applied']
        assert len(applied)==1


def test_cancelled_change_cannot_publish_and_leaves_head(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'取消的变更',edited(base,lambda n:n['name']=='north-replica',{'delay_ms':1}))
    assert client.post('/topology/changes/%d/cancel'%change['id'],headers=h).status_code==200
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-cancel-01'},headers=h).status_code==409
    assert current_nodes(client,h)==base


def test_draft_edit_conflicts_on_stale_revision(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'并发编辑',base)
    first=client.put('/topology/changes/%d'%change['id'],json={'nodes':edited(base,lambda n:n['name']=='north-replica',{'note':'A'}),'revision':change['revision']},headers=h)
    assert first.status_code==200
    stale=client.put('/topology/changes/%d'%change['id'],json={'nodes':edited(base,lambda n:n['name']=='north-replica',{'note':'B'}),'revision':change['revision']},headers=h)
    assert stale.status_code==409
    assert next(n for n in client.get('/topology/changes/%d'%change['id'],headers=h).json()['draft_nodes'] if n['name']=='north-replica')['note']=='A'


def test_resolution_is_invalidated_when_either_side_changes_again(client):
    h=auth(client,'admin')
    base=current_nodes(client,h)
    change=draft(client,h,'冲突再变化',edited(base,lambda n:n['name']=='south-replica',{'note':'草稿版'}))
    south=next(n for n in base if n['name']=='south-replica')
    live_payload={k:south[k] for k in ('name','role','status','lineage','position','delay_ms','note')}
    client.put('/nodes/%d'%south['id'],json={**live_payload,'note':'当前版一'},headers=h)
    resolved=client.post('/topology/changes/%d/resolve'%change['id'],json={'resolutions':[{'node':'south-replica','choice':'draft'}]},headers=h)
    assert resolved.json()['unresolved_count']==0
    # Current side changes again after resolution: the recorded choice no longer applies.
    client.put('/nodes/%d'%south['id'],json={**live_payload,'note':'当前版二'},headers=h)
    after=client.get('/topology/changes/%d'%change['id'],headers=h).json()
    assert after['unresolved_count']==1
    assert after['conflicts'][0]['current']['note']=='当前版二'
    assert after['resolution_history']  # both sides' evidence kept for review
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-reinv-01','confirm_stale_base':True},headers=h).status_code==409
    # Re-resolving the new state unblocks it.
    ok=client.post('/topology/changes/%d/resolve'%change['id'],json={'resolutions':[{'node':'south-replica','choice':'draft','note':'再次核对'}]},headers=h)
    assert ok.json()['unresolved_count']==0
    assert client.post('/topology/changes/%d/publish'%change['id'],json={'request_id':'req-reinv-02','confirm_stale_base':True},headers=h).status_code==200
    assert next(n for n in current_nodes(client,h) if n['name']=='south-replica')['note']=='草稿版'
