from fastapi import APIRouter, Depends
from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.topology import ChangeCreate, ChangeUpdate, ResolveInput, PublishInput
from src.services import topology as service

router = APIRouter()


@router.get('/topology')
def topology(db=Depends(db_session), user=Depends(current)):
    return service.topology(db)


@router.get('/topology/versions')
def versions(db=Depends(db_session), user=Depends(current)):
    return service.versions(db)


@router.get('/topology/versions/{ident}')
def version(ident: int, db=Depends(db_session), user=Depends(current)):
    from fastapi import HTTPException
    obj = service.get_version(db, ident)
    if not obj:
        raise HTTPException(404, '拓扑版本不存在')
    return service.version_detail(obj)


@router.get('/topology/changes')
def changes(db=Depends(db_session), user=Depends(current)):
    return service.change_listing(db)


@router.post('/topology/changes', status_code=201)
def create_change(data: ChangeCreate, db=Depends(db_session), user=Depends(writer)):
    return service.create_change(db, data, user)


@router.get('/topology/changes/{ident}')
def change(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.change_detail(db, ident)


@router.put('/topology/changes/{ident}')
def update_change(ident: int, data: ChangeUpdate, db=Depends(db_session), user=Depends(writer)):
    return service.update_change(db, ident, data, user)


@router.post('/topology/changes/{ident}/validate')
def validate_change(ident: int, db=Depends(db_session), user=Depends(writer)):
    return service.validate_change(db, ident, user)


@router.post('/topology/changes/{ident}/resolve')
def resolve_change(ident: int, data: ResolveInput, db=Depends(db_session), user=Depends(writer)):
    return service.resolve_change(db, ident, data, user)


@router.post('/topology/changes/{ident}/publish')
def publish_change(ident: int, data: PublishInput, db=Depends(db_session), user=Depends(writer)):
    return service.publish_change(db, ident, data, user)


@router.post('/topology/changes/{ident}/cancel')
def cancel_change(ident: int, db=Depends(db_session), user=Depends(writer)):
    return service.cancel_change(db, ident, user)


@router.post('/topology/recover')
def recover(db=Depends(db_session), user=Depends(writer)):
    return {'recovered': service.recover(db)}
