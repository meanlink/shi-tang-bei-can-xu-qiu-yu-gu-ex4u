from pydantic import BaseModel, Field


class DispatchInput(BaseModel):
    job_id: int
    title: str = Field(default='已批准备餐单', min_length=2, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=120)
    is_drill: bool = False
    auto_advance: bool = True
    simulation: dict = Field(default_factory=dict)


class CancelInput(BaseModel):
    reason: str = Field(default='计划变更', max_length=300)


class ManualResolveInput(BaseModel):
    note: str = Field(min_length=2, max_length=500)
    outcome: str = Field(default='acknowledged', pattern='^(acknowledged|stock_used|settled)$')
