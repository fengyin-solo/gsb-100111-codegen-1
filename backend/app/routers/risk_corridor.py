"""风险走廊接口：按里程展开走廊段，承载现场复核、等级裁定、封控发布与派单处置。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.risk_corridor import RiskCorridorService

router = APIRouter(prefix="/api/risk_corridor", tags=["风险走廊"])

service = RiskCorridorService()


@router.get("/summary", response_model=dict)
def corridor_summary() -> dict[str, Any]:
    """总览看板卡片：读取聚合投影，与巡查台账、病害清单共用同一裁定版本。"""
    return service.summary()


@router.get("/events", response_model=dict)
def replay_events(
    after: int = Query(default=0, ge=0, description="事件游标：只返回游标之后的事件"),
    limit: int = Query(default=100, description="单次重放条数"),
) -> dict[str, Any]:
    """事件重放：连接断开后客户端带着旧游标继续，不丢不重。"""
    if limit > 500:
        raise HTTPException(status_code=400, detail="单次重放最多 500 条，请缩小范围")
    events, latest = service.list_events(after=after, limit=limit)
    return {"events": events, "latest": latest}


@router.get("/closures", response_model=dict)
def list_closures() -> dict[str, Any]:
    """历史封控记录：封控等级按发布时的裁定等级保留。"""
    items = service.list_closures()
    return {"total": len(items), "items": items}


@router.get("", response_model=PageResult[dict])
def list_segments(
    keyword: str | None = Query(default=None, description="按走廊段编号或路段名称检索"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """按里程展开风险走廊，每段带出病害等级、巡查逾期与桥梁限载提示。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    views = service.list_segments()
    if keyword:
        views = [
            view for view in views
            if keyword in str(view["走廊段编号"]) or keyword in str(view["路段名称"])
        ]
    total = len(views)
    start = max(page - 1, 0) * size
    return PageResult(items=views[start:start + size], total=total, page=page, size=size)


@router.post("/{segment_id}/actions", response_model=ActionResult)
def run_action(segment_id: int, payload: EntryPayload) -> ActionResult:
    """推进走廊段：单向流转、跳级拦截、重复上报去重，不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(segment_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
