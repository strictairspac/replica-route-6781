from typing import Literal
from pydantic import BaseModel, Field, model_validator


class TopologyNode(BaseModel):
    id: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=2, max_length=80)
    role: Literal['primary', 'replica']
    status: Literal['healthy', 'down'] = 'healthy'
    lineage: str = Field(min_length=2, max_length=80)
    position: int = Field(ge=0, le=1000000000)
    delay_ms: int = Field(ge=0, le=3600000)
    note: str = Field(default='', max_length=300)


class ChangeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    nodes: list[TopologyNode] = Field(min_length=1, max_length=12)


class ChangeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    nodes: list[TopologyNode] = Field(min_length=1, max_length=12)
    revision: int | None = Field(default=None, ge=1)


class Resolution(BaseModel):
    node: str = Field(min_length=2, max_length=80)
    choice: Literal['draft', 'current', 'custom']
    value: TopologyNode | None = None
    note: str = Field(default='', max_length=300)

    @model_validator(mode='after')
    def value_required(self):
        if self.choice == 'custom' and self.value is None:
            raise ValueError('选择custom时必须提供完整节点内容')
        if self.choice != 'custom' and self.value is not None:
            raise ValueError('仅custom可附带节点内容')
        return self


class ResolveInput(BaseModel):
    resolutions: list[Resolution] = Field(min_length=1, max_length=12)


class PublishInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=80)
    confirm_stale_base: bool = False
