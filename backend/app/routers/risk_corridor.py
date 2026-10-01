"""风险走廊接口：按里程展开的总览、单向推进动作、事件上报与游标重放。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, CorridorEventPayload, EntryPayload
from app.services.risk_corridor import risk_corridor_service

router = APIRouter(prefix="/api/risk-corridor", tags=["风险走廊"])

service = risk_corridor_service


@router.get("/segments")
def list_segments(
    keyword: str | None = Query(default=None, description="按路段名称或桩号检索"),
    stage: str | None = Query(default=None, description="待复核、已复核、已裁定、已封控"),
) -> dict[str, Any]:
    """按里程顺序列出走廊段：每段带待处置病害等级、巡查逾期与桥梁限载提示。"""
    items = service.list_segments(stage=stage, keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/overview")
def overview() -> dict[str, Any]:
    """总览看板：读取与巡查台账、病害待办同一裁定版本的增量投影。"""
    return service.overview()


@router.get("/events")
def replay_events(
    after: int = Query(default=0, description="事件游标，只返回游标之后的事件"),
    limit: int = Query(default=100, description="单次重放条数"),
) -> dict[str, Any]:
    """事件重放：连接断开后按事件游标继续，不丢不重。"""
    if limit > 500:
        raise HTTPException(status_code=400, detail="单次重放最多 500 条，请按游标分批续传")
    return service.events_after(after, limit)


@router.post("/events", response_model=ActionResult)
def report_event(payload: CorridorEventPayload) -> ActionResult:
    """接收病害、巡查逾期、限载裁定上报；按 event_id 幂等，重复上报不累加。"""
    event, message = service.report(payload.event_id, payload.type, payload.segment_id, payload.payload)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=event)


@router.post("/segments/{segment_id}/actions", response_model=ActionResult)
def run_action(segment_id: int, payload: EntryPayload) -> ActionResult:
    """从风险桩号发起现场复核、等级裁定、封控发布、派单、处置结论；越级动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(segment_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
