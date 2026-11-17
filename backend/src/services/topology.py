"""Whole-topology changes.

The authoritative live configuration is one immutable ``TopologyVersion`` row.
Every topology write (quick single-node edit or a published change) creates a
new complete version and moves ``TopologyHead`` inside the same transaction, so
readers can only ever observe the full before-set or the full after-set — never
an intermediate set with zero or two primaries.
"""
import copy, hashlib, json, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, func
from src.models.entities import Node, TopologyVersion, TopologyHead, TopologyChange, Audit
from src.utils.serialization import record

NODE_FIELDS = ('id', 'name', 'role', 'status', 'lineage', 'position', 'delay_ms', 'note')
CONTENT_FIELDS = ('name', 'role', 'status', 'lineage', 'position', 'delay_ms', 'note')
CHANGE_STATES = ('draft', 'applying', 'applied', 'failed', 'cancelled')
CONFLICT_KINDS = {
    'modified': '双方以不同方式修改同一节点',
    'removed_vs_modified': '草稿删除节点，但期间该节点已被修改',
    'modified_vs_removed': '草稿修改节点，但期间该节点已被删除',
    'added_both': '草稿与期间变更各自新增了同名节点且内容不同',
}
_SQLITE_LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc)


def iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def norm(raw):
    """Normalise a node dict (model, ORM record or JSON) to the stored shape."""
    if raw is None:
        return None
    data = raw.model_dump() if hasattr(raw, 'model_dump') else (record(raw) if hasattr(raw, '__table__') else dict(raw))
    node = {}
    for field in NODE_FIELDS:
        value = data.get(field)
        if field in ('position', 'delay_ms', 'id') and value is not None and value != '':
            value = int(value)
        node[field] = value
    node['status'] = node.get('status') or 'healthy'
    node['note'] = node.get('note') or ''
    return node


def canonical(node):
    return json.dumps({k: (node or {}).get(k) for k in CONTENT_FIELDS}, sort_keys=True, ensure_ascii=False)


def nodes_checksum(nodes):
    payload = [{k: n.get(k) for k in CONTENT_FIELDS} for n in sorted(nodes, key=lambda x: x['name'])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_nodes(raw_nodes):
    """One primary, unique names/ids, lineage and position rules — checked on the whole set."""
    if not isinstance(raw_nodes, list):
        return ['拓扑必须是节点数组']
    nodes = [norm(n) for n in raw_nodes]
    errors = []
    if not 1 <= len(nodes) <= 12:
        errors.append('拓扑需要1–12个节点，当前 %d 个' % len(nodes))
    names, ids, primaries = [], [], []
    for index, node in enumerate(nodes):
        label = node.get('name') or ('第%d个节点' % (index + 1))
        name = (node.get('name') or '').strip()
        if len(name) < 2 or len(name) > 80:
            errors.append('%s：节点名需2–80个字符' % label)
        names.append(name)
        if node.get('id') is not None:
            ids.append(node['id'])
        if node.get('role') not in ('primary', 'replica'):
            errors.append('%s：角色必须是primary或replica' % label)
        if node.get('role') == 'primary':
            primaries.append(node)
        if node.get('status') not in ('healthy', 'down'):
            errors.append('%s：状态必须是healthy或down' % label)
        if len(node.get('lineage') or '') < 2:
            errors.append('%s：日志谱系需2–80个字符' % label)
        position = node.get('position')
        if not isinstance(position, int) or not 0 <= position <= 1000000000:
            errors.append('%s：位点必须是0–1000000000的整数' % label)
        delay = node.get('delay_ms')
        if not isinstance(delay, int) or not 0 <= delay <= 3600000:
            errors.append('%s：延迟必须是0–3600000的整数' % label)
        if len(node.get('note') or '') > 300:
            errors.append('%s：备注不得超过300个字符' % label)
    duplicates = sorted({n for n in names if n and names.count(n) > 1})
    if duplicates:
        errors.append('节点名必须唯一，重复：' + '、'.join(duplicates))
    if len(ids) != len(set(ids)):
        errors.append('节点ID必须唯一')
    if len(primaries) != 1:
        errors.append('必须且只能配置一个主节点，当前 %d 个' % len(primaries))
    elif all(isinstance(n.get('position'), int) for n in nodes):
        primary = primaries[0]
        for node in nodes:
            if node.get('role') == 'replica' and node.get('lineage') == primary.get('lineage') and node.get('position') > primary.get('position'):
                errors.append('%s：同谱系副本位点不得高于主库初始位点 %d' % (node.get('name'), primary['position']))
    return errors


# ---------------------------------------------------------------- head locking

def _head(db, lock=False):
    query = select(TopologyHead).order_by(TopologyHead.id)
    head = db.scalar(query.with_for_update() if lock else query)
    if head is None:
        # Fixed primary key: concurrent first-writers collide instead of creating
        # two head rows. The savepoint keeps the caller's work intact on collision.
        try:
            with db.begin_nested():
                db.add(TopologyHead(id=1, head_version_id=None))
            db.flush()
        except Exception:
            pass
    return db.scalar(query.with_for_update() if lock else query)


@contextmanager
def head_lock(db):
    """Serialise topology writers.

    MySQL takes a row lock on the single head row. SQLite has no row locks, so
    the in-process lock keeps writers serialised for the local test harness.
    Callers MUST commit or roll back inside the block.
    """
    dialect = db.bind.dialect.name if db.bind is not None else 'mysql'
    if dialect == 'sqlite':
        with _SQLITE_LOCK:
            yield _head(db, lock=False)
    else:
        yield _head(db, lock=True)


def get_version(db, ident):
    return db.get(TopologyVersion, ident) if ident else None


def version_summary(obj):
    data = record(obj)
    data['node_count'] = len(obj.nodes or [])
    return data


def version_detail(obj):
    data = version_summary(obj)
    data['nodes'] = obj.nodes
    return data


def current(db):
    head = _head(db)
    if not head.head_version_id:
        bootstrap(db)
        head = _head(db)
    version = get_version(db, head.head_version_id)
    if version is None:
        bootstrap(db)
        version = get_version(db, _head(db).head_version_id)
    return version


def current_nodes(db):
    return copy.deepcopy(current(db).nodes)


def bootstrap(db):
    """Create v1 from the legacy nodes table if no version exists yet.

    Held under the same writer lock as every other topology write so two
    processes starting together cannot both mint v1.
    """
    with head_lock(db):
        head = _head(db)
        if head.head_version_id:
            return get_version(db, head.head_version_id)
        nodes = [norm(x) for x in db.scalars(select(Node).order_by(Node.id))]
        if not nodes:
            db.commit()
            return None
        version = _write_version(db, nodes, 'bootstrap', None, 'system')
        db.add(Audit(actor='system', action='初始化整套拓扑版本 v%d' % version.version))
        db.commit()
        return version


def _write_version(db, nodes, source, change_id, actor):
    head = _head(db)
    latest = db.scalar(select(func.max(TopologyVersion.version))) or 0
    ordered = sorted([norm(n) for n in nodes], key=lambda n: (n['id'] if n.get('id') is not None else 0, n['name']))
    version = TopologyVersion(version=latest + 1, nodes=ordered, checksum=nodes_checksum(ordered),
                              source=source, change_id=change_id, created_by=actor, created_at=now())
    db.add(version)
    db.flush()
    _sync_nodes(db, ordered)
    head.head_version_id = version.id
    head.updated_at = now()
    db.flush()
    return version


def _sync_nodes(db, nodes):
    existing = {obj.id: obj for obj in db.scalars(select(Node))}
    seen = set()
    for node in nodes:
        ident = node.get('id')
        values = {k: node.get(k) for k in CONTENT_FIELDS}
        obj = existing.get(ident)
        if obj is None:
            obj = Node(id=ident, **values)
            db.add(obj)
        else:
            for key, value in values.items():
                setattr(obj, key, value)
        seen.add(ident)
    for ident, obj in existing.items():
        if ident not in seen:
            db.delete(obj)
    db.flush()


def apply_quick(db, user, action, mutate):
    """Atomic single-node convenience edit, always on a complete version."""
    try:
        if not _head(db).head_version_id:
            bootstrap(db)
        with head_lock(db):
            version = current(db)
            nodes = copy.deepcopy(version.nodes)
            nodes = mutate(nodes)
            errors = validate_nodes(nodes)
            if errors:
                raise HTTPException(422, '；'.join(errors) + '。主副角色调整请使用「整套拓扑变更」。')
            nodes = _assign_ids(nodes, version.nodes)
            new_version = _write_version(db, nodes, 'quick', None, user.email)
            db.add(Audit(actor=user.email, action=action + '（拓扑 v%d → v%d）' % (version.version, new_version.version)))
            db.commit()
            return new_version
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _assign_ids(nodes, known_nodes):
    known = {}
    for node in known_nodes or []:
        if node.get('name') and node.get('id') is not None:
            known[node['name']] = node['id']
    used = set(known.values())
    next_id = max(used, default=0) + 1
    result = []
    for node in nodes:
        node = norm(node)
        ident = known.get(node.get('name'))
        if ident is None:
            while next_id in used:
                next_id += 1
            ident = next_id
            used.add(ident)
            next_id += 1
        node['id'] = ident
        result.append(node)
    return sorted(result, key=lambda n: (n['id'], n['name']))


# ------------------------------------------------------------- versions / drafts

def versions(db):
    return [version_summary(x) for x in db.scalars(select(TopologyVersion).order_by(TopologyVersion.version.desc()))]


def topology(db):
    version = current(db)
    data = version_detail(version) if version else {'version': 0, 'nodes': [], 'checksum': None}
    return {'head': data, 'versions': versions(db)}


# --------------------------------------------------------------- conflict merge

def detect_conflicts(base_nodes, draft_nodes, current_nodes):
    """Three-way detection. A node the draft never touched always keeps the
    live value, so a draft can never silently overwrite an intervening edit."""
    base = {n['name']: norm(n) for n in base_nodes or []}
    draft = {n['name']: norm(n) for n in draft_nodes or []}
    live = {n['name']: norm(n) for n in current_nodes or []}
    conflicts = []
    for name in sorted(set(base) | set(draft) | set(live)):
        b, d, c = base.get(name), draft.get(name), live.get(name)
        if canonical(d) == canonical(b):
            continue  # draft made no statement: live value wins automatically
        if canonical(c) == canonical(d) or canonical(c) == canonical(b):
            continue  # both agree, or the draft still applies cleanly
        if b and d and c:
            kind = 'modified'
        elif b and not d and c:
            kind = 'removed_vs_modified'
        elif b and d and not c:
            kind = 'modified_vs_removed'
        else:
            kind = 'added_both'
        conflicts.append({'node': name, 'kind': kind, 'kind_label': CONFLICT_KINDS[kind],
                          'base': b, 'draft': d, 'current': c,
                          # Fingerprint covers both sides: editing either the draft or
                          # the live value invalidates a stale resolution and forces re-review.
                          'fingerprint': canonical(c) + '|' + canonical(d),
                          'current_version_id': None, 'current_version': None,
                          'base_version_id': None, 'resolution': None})
    return conflicts


def _enrich(db, conflicts, base_version_id, live_version):
    for conflict in conflicts:
        conflict['base_version_id'] = base_version_id
        conflict['current_version_id'] = live_version.id
        conflict['current_version'] = live_version.version
        conflict['current_checksum'] = live_version.checksum
    return conflicts


def intervening_versions(db, change):
    query = select(TopologyVersion).where(TopologyVersion.id > (change.base_version_id or 0),
                                         TopologyVersion.id <= (current(db).id)).order_by(TopologyVersion.id)
    return [version_summary(x) for x in db.scalars(query)]


def _attach_resolutions(conflicts, stored):
    stored = {c['node']: c for c in (stored or []) if c.get('resolution')}
    for conflict in conflicts:
        previous = stored.get(conflict['node'])
        resolution = previous and previous.get('resolution')
        if resolution and resolution.get('fingerprint') == conflict['fingerprint']:
            conflict['resolution'] = resolution
    return conflicts


def merge_nodes(base_nodes, draft_nodes, current_nodes, conflicts):
    base = {n['name']: norm(n) for n in base_nodes or []}
    draft = {n['name']: norm(n) for n in draft_nodes or []}
    live = {n['name']: norm(n) for n in current_nodes or []}
    result = dict(draft)
    for name in set(base) | set(live):
        b, d, c = base.get(name), draft.get(name), live.get(name)
        if canonical(d) == canonical(b):
            if c is None:
                result.pop(name, None)
            else:
                result[name] = c
    unresolved = []
    for conflict in conflicts:
        resolution = conflict.get('resolution')
        if not resolution:
            unresolved.append(conflict)
            continue
        choice = resolution.get('choice')
        if choice == 'current':
            value = conflict.get('current')
        elif choice == 'custom':
            value = norm(resolution.get('value'))
        else:
            value = conflict.get('draft')
        if value is None:
            result.pop(conflict['node'], None)
        else:
            result[conflict['node']] = value
    return list(result.values()), unresolved


def _evaluate(db, change):
    """Recompute conflicts against the current head; never trusts stale drafts."""
    live_version = current(db)
    conflicts = detect_conflicts(change.base_nodes, change.draft_nodes, live_version.nodes)
    conflicts = _enrich(db, conflicts, change.base_version_id, live_version)
    conflicts = _attach_resolutions(conflicts, change.conflicts)
    merged, unresolved = merge_nodes(change.base_nodes, change.draft_nodes, live_version.nodes, conflicts)
    errors = validate_nodes(merged) if not unresolved else validate_nodes(change.draft_nodes)
    change.conflicts = conflicts
    change.validation_errors = errors
    return live_version, conflicts, unresolved, merged, errors


def _change_detail(db, change, evaluate=True):
    live_version = current(db)
    if evaluate and change.status in ('draft', 'applying', 'failed'):
        _evaluate(db, change)
    data = record(change)
    data['live_version'] = version_summary(live_version) if live_version else None
    data['stale_base'] = bool(live_version and change.base_version_id != live_version.id)
    data['status_label'] = {'draft': '草稿', 'applying': '切换中', 'applied': '已生效',
                            'failed': '失败', 'cancelled': '已取消'}.get(change.status, change.status)
    data['base_version'] = version_summary(get_version(db, change.base_version_id)) if get_version(db, change.base_version_id) else None
    if change.applied_version_id:
        applied = get_version(db, change.applied_version_id)
        data['applied_version'] = applied and version_summary(applied)
    data['unresolved_count'] = len([c for c in (change.conflicts or []) if not c.get('resolution')])
    return data


def change_listing(db):
    return [_change_detail(db, x, evaluate=False) for x in db.scalars(select(TopologyChange).order_by(TopologyChange.id.desc()))]


def change_detail(db, ident):
    change = db.get(TopologyChange, ident)
    if not change:
        raise HTTPException(404, '拓扑变更不存在')
    detail = _change_detail(db, change)
    if change.status in ('draft', 'applying', 'failed'):
        db.commit()
    detail['intervening_versions'] = intervening_versions(db, change) if change.status != 'applied' else []
    return detail


def create_change(db, data, user):
    try:
        if not _head(db).head_version_id:
            bootstrap(db)
        with head_lock(db):
            live_version = current(db)
            base = copy.deepcopy(live_version.nodes)
            incoming = data.nodes if data.nodes is not None else base
            nodes = _assign_ids([norm(n) for n in incoming], base)
            change = TopologyChange(name=data.name, status='draft', base_version_id=live_version.id,
                                    base_nodes=base, draft_nodes=nodes, conflicts=[], validation_errors=[],
                                    resolution_history=[], apply_plan={'attempts': []}, attempts=0, revision=1,
                                    created_by=user.email, created_at=now(), updated_at=now())
            db.add(change)
            db.add(Audit(actor=user.email, action='创建整套拓扑变更草稿：' + data.name))
            db.commit()
            return _change_detail(db, change, evaluate=False)
    except HTTPException:
        db.rollback()
        raise


def _editable(change):
    if change.status not in ('draft', 'failed'):
        raise HTTPException(409, '当前状态为 %s，只有草稿或失败记录可以调整' % change.status)


def update_change(db, ident, data, user):
    try:
        with head_lock(db):
            change = db.get(TopologyChange, ident)
            if not change:
                raise HTTPException(404, '拓扑变更不存在')
            _editable(change)
            if data.revision is not None and data.revision != change.revision:
                raise HTTPException(409, '草稿已被其他人修改，请刷新后基于最新草稿调整（当前修订 %d）' % change.revision)
            known = (change.base_nodes or []) + (change.draft_nodes or [])
            nodes = _assign_ids([norm(n) for n in data.nodes], known)
            name = data.name or change.name
            unchanged = name == change.name and nodes == [norm(n) for n in change.draft_nodes]
            change.name = name
            change.draft_nodes = nodes
            if not unchanged:
                change.revision += 1
            change.error = None
            change.updated_at = now()
            _evaluate(db, change)
            db.add(Audit(actor=user.email, action='调整整套拓扑变更草稿：' + change.name))
            db.commit()
            return _change_detail(db, change, evaluate=False)
    except HTTPException:
        db.rollback()
        raise


def validate_change(db, ident, user):
    try:
        with head_lock(db):
            change = db.get(TopologyChange, ident)
            if not change:
                raise HTTPException(404, '拓扑变更不存在')
            live_version, conflicts, unresolved, merged, errors = _evaluate(db, change)
            db.commit()
            detail = _change_detail(db, change, evaluate=False)
            detail['live_version_id'] = live_version.id
            detail['live_version'] = version_summary(live_version)
            detail['stale_base'] = change.base_version_id != live_version.id
            detail['intervening_versions'] = intervening_versions(db, change)
            detail['publishable'] = not errors and not unresolved
            return detail
    except HTTPException:
        db.rollback()
        raise


def resolve_change(db, ident, data, user):
    try:
        with head_lock(db):
            change = db.get(TopologyChange, ident)
            if not change:
                raise HTTPException(404, '拓扑变更不存在')
            _editable(change)
            live_version, conflicts, _, _, _ = _evaluate(db, change)
            by_node = {c['node']: c for c in conflicts}
            history = list(change.resolution_history or [])
            for item in data.resolutions:
                conflict = by_node.get(item.node)
                if not conflict:
                    raise HTTPException(409, '节点 %s 当前没有需要处理的冲突，请刷新后重试' % item.node)
                if item.choice == 'custom' and item.value is None:
                    raise HTTPException(422, '选择 custom 时必须提供节点内容（删除请选 draft 或 current）')
                resolution = {'choice': item.choice, 'note': item.note,
                              'value': norm(item.value) if item.value is not None else None,
                              'fingerprint': conflict['fingerprint'], 'actor': user.email, 'at': iso(now())}
                conflict['resolution'] = resolution
                history.append({'node': item.node, 'kind': conflict['kind'], 'base': conflict['base'],
                                'draft': conflict['draft'], 'current': conflict['current'],
                                'choice': item.choice, 'value': resolution['value'], 'note': item.note,
                                'actor': user.email, 'at': resolution['at']})
            changed = {item.node for item in data.resolutions}
            change.conflicts = conflicts
            change.resolution_history = history
            change.updated_at = now()
            merged = None
            if not [c for c in conflicts if not c.get('resolution')]:
                merged, _ = merge_nodes(change.base_nodes, change.draft_nodes, live_version.nodes, conflicts)
                change.validation_errors = validate_nodes(merged)
            else:
                change.validation_errors = []
            db.add(Audit(actor=user.email, action='处理拓扑变更冲突：' + change.name + '（' + '、'.join(sorted(changed)) + '）'))
            db.commit()
            detail = _change_detail(db, change, evaluate=False)
            detail['live_version'] = version_summary(live_version)
            detail['stale_base'] = change.base_version_id != live_version.id
            detail['intervening_versions'] = intervening_versions(db, change)
            detail['publishable'] = not change.validation_errors and all(c.get('resolution') for c in conflicts)
            return detail
    except HTTPException:
        db.rollback()
        raise


def cancel_change(db, ident, user):
    change = db.get(TopologyChange, ident)
    if not change:
        raise HTTPException(404, '拓扑变更不存在')
    if change.status == 'applied':
        raise HTTPException(409, '已生效的变更不能取消')
    change.status = 'cancelled'
    change.updated_at = now()
    db.add(Audit(actor=user.email, action='取消整套拓扑变更：' + change.name))
    db.commit()
    return _change_detail(db, change, evaluate=False)


def _attempts(change):
    plan = change.apply_plan or {}
    return list(plan.get('attempts') or [])


def _mark_attempt(change, request_id, outcome, **extra):
    plan = dict(change.apply_plan or {})
    attempts = list(plan.get('attempts') or [])
    target = None
    if request_id:
        for attempt in reversed(attempts):
            if attempt.get('request_id') == request_id and attempt.get('outcome') == 'running':
                target = attempt
                break
    else:
        for attempt in reversed(attempts):
            if attempt.get('outcome') == 'running':
                target = attempt
                break
    if target is not None:
        target['outcome'] = outcome
        target.update(extra)
    else:
        attempts.append(dict({'request_id': request_id, 'outcome': outcome, **extra}))
    plan['attempts'] = attempts
    change.apply_plan = plan


def recover(db):
    """Resume interrupted changes in place: never create a second record."""
    _head(db)
    db.commit()
    recovered = []
    for change in db.scalars(select(TopologyChange).where(TopologyChange.status == 'applying')):
        version = db.scalar(select(TopologyVersion).where(TopologyVersion.change_id == change.id))
        if version:
            change.status = 'applied'
            change.applied_version_id = version.id
            change.applied_at = version.created_at
            change.error = None
            _mark_attempt(change, None, 'applied', version_id=version.id, recovered=True)
        else:
            change.status = 'draft'
            change.error = '上次切换在提交前中断，已恢复为草稿。重试将沿用本记录与原始基准，不会重复生效。'
            _mark_attempt(change, None, 'interrupted')
        change.updated_at = now()
        recovered.append(change.id)
    db.commit()
    return recovered


def startup():
    """Bootstrap v1 and finish any switch that was interrupted mid-flight."""
    from src.config.database import Session
    with Session() as db:
        try:
            head = _head(db)
            db.commit()
            if not head.head_version_id:
                bootstrap(db)
            return recover(db)
        except Exception:
            db.rollback()
            raise


def publish_change(db, ident, data, user):
    change = db.get(TopologyChange, ident)
    if not change:
        raise HTTPException(404, '拓扑变更不存在')
    if change.status == 'applied':
        return _change_detail(db, change, evaluate=False)
    if change.status == 'cancelled':
        raise HTTPException(409, '该变更已取消，不能发布')
    for attempt in _attempts(change):
        if data.request_id and attempt.get('request_id') == data.request_id and attempt.get('outcome') == 'applied':
            return _change_detail(db, change, evaluate=False)

    # Phase 1: durably mark the attempt so an interrupted switch is recognisable
    # on restart and is resumed with this same record.
    change.status = 'applying'
    change.attempts = (change.attempts or 0) + 1
    change.error = None
    change.updated_at = now()
    head_at_start = current(db).id
    _attempts_plan = dict(change.apply_plan or {})
    _attempts_plan['head_at_start'] = head_at_start
    _attempts_plan['attempts'] = list(_attempts_plan.get('attempts') or []) + [
        {'request_id': data.request_id, 'outcome': 'running', 'actor': user.email, 'started_at': iso(now()),
         'head_at_start': head_at_start}]
    change.apply_plan = _attempts_plan
    db.add(Audit(actor=user.email, action='开始切换整套拓扑变更：' + change.name))
    db.commit()

    try:
        with head_lock(db):
            change = db.get(TopologyChange, ident)
            live_version = current(db)
            existing = db.scalar(select(TopologyVersion).where(TopologyVersion.change_id == change.id))
            plan = dict(change.apply_plan or {})
            head_at_start = plan.get('head_at_start')
            if existing:
                # A previous attempt committed; this retry must not re-apply.
                change.status = 'applied'
                change.applied_version_id = existing.id
                change.applied_at = existing.created_at
                change.error = None
                _mark_attempt(change, data.request_id, 'applied', version_id=existing.id, idempotent=True)
                db.commit()
                return _change_detail(db, change, evaluate=False)
            if head_at_start is not None and live_version.id != head_at_start:
                # Someone else published while this change was mid-switch; refuse
                # rather than clobber a topology this change never saw.
                raise HTTPException(409, '切换期间拓扑已被其他变更推进到 v%d，置为冲突待处理，本次未生效。请重新校验后发布。' % live_version.version)
            competing = db.scalar(select(TopologyChange).where(TopologyChange.status == 'applying',
                                                              TopologyChange.id != change.id))
            if competing:
                raise HTTPException(409, '变更「%s」正在切换，同一时刻只允许一个方案生效。请等待其结果后重试。' % competing.name)

            conflicts = detect_conflicts(change.base_nodes, change.draft_nodes, live_version.nodes)
            conflicts = _enrich(db, conflicts, change.base_version_id, live_version)
            conflicts = _attach_resolutions(conflicts, change.conflicts)
            merged, unresolved = merge_nodes(change.base_nodes, change.draft_nodes, live_version.nodes, conflicts)
            change.conflicts = conflicts
            stale = change.base_version_id != live_version.id

            if stale and not data.confirm_stale_base:
                raise HTTPException(409, '基准拓扑已从 v%d 前进到 v%d；草稿期间已有修改生效。请查看冲突双方依据并确认基于当前拓扑合并后再发布。'
                                    % (get_version(db, change.base_version_id).version if get_version(db, change.base_version_id) else 0, live_version.version))
            if unresolved:
                names = '、'.join(c['node'] for c in unresolved)
                raise HTTPException(409, '存在未处理冲突：%s。请先保留草稿或当前版本的依据并作出选择。' % names)
            errors = validate_nodes(merged)
            change.validation_errors = errors
            if errors:
                raise HTTPException(422, '；'.join(errors))
            nodes = _assign_ids(merged, live_version.nodes)
            version = _write_version(db, nodes, 'change', change.id, user.email)
            change.status = 'applied'
            change.applied_version_id = version.id
            change.applied_at = now()
            change.error = None
            change.updated_at = now()
            plan = dict(change.apply_plan or {})
            plan.update({'head_before': live_version.id, 'head_before_version': live_version.version,
                         'head_after': version.id, 'head_after_version': version.version,
                         'merged_checksum': version.checksum, 'stale_base_rebased': stale, 'conflicts': conflicts})
            change.apply_plan = plan
            _mark_attempt(change, data.request_id, 'applied', version_id=version.id)
            db.add(Audit(actor=user.email, action='整套拓扑变更生效：%s（v%d → v%d，原子切换）'
                                      % (change.name, live_version.version, version.version)))
            db.commit()
            return _change_detail(db, change, evaluate=False)
    except HTTPException as exc:
        db.rollback()
        change = db.get(TopologyChange, ident)
        change.status = 'draft'
        change.error = str(exc.detail)
        change.updated_at = now()
        _mark_attempt(change, data.request_id, 'blocked', reason=str(exc.detail))
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        change = db.get(TopologyChange, ident)
        if change and change.status != 'applied':
            change.status = 'failed'
            change.error = '切换未提交：%s。变更记录已保留，可对同一条记录重试。' % exc
            change.updated_at = now()
            _mark_attempt(change, data.request_id, 'failed', reason=str(exc))
            db.commit()
        raise HTTPException(500, '切换未提交，记录已保留，可重试同一条变更')
