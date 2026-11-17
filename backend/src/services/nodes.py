from fastapi import HTTPException
from src.services import topology


def listing(db):
    version = topology.current(db)
    return version.nodes if version else []


def _live_nodes(db):
    version = topology.current(db)
    return list(version.nodes) if version else []


def _by_name(nodes, name):
    for node in nodes:
        if node.get('name') == name:
            return node
    return None


def create(db, data, user):
    """Single-node convenience path: still validates and writes a whole version."""
    def mutate(nodes):
        if any(n.get('name') == data.name for n in nodes):
            raise HTTPException(409, '节点名已存在')
        nodes.append(topology.norm(data))
        return nodes
    version = topology.apply_quick(db, user, '创建复制节点 ' + data.name, mutate)
    return _by_name(version.nodes, data.name)


def update(db, ident, data, user):
    def mutate(nodes):
        for index, existing in enumerate(nodes):
            if existing.get('id') == ident:
                nodes[index] = {**topology.norm(data), 'id': ident}
                return nodes
        raise HTTPException(404, '节点不存在')
    version = topology.apply_quick(db, user, '更新复制节点 ' + data.name, mutate)
    if any(n.get('name') == data.name for n in version.nodes):
        return _by_name(version.nodes, data.name)
    return {'id': ident, **data.model_dump()}
