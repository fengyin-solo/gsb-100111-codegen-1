"""风险走廊业务规则：按里程展开的风险段、单向推进的处置流程与事件溯源投影。

设计约定：
- 走廊段按「现场复核 → 等级裁定 → 封控发布」单向推进，未复核不得跳级封控；
- 一切状态变化先写事件日志（带游标），再在同一把锁内增量投影到走廊段与看板聚合，
  事件落库与投影更新处在同一事务边界：校验不通过则什么都不写，写则一起生效；
- 上报事件按 event_id 去重、病害按病害编号去重，重复上报不会累加；
  连接断开后用事件游标（after）续传重放；
- 等级冲突时以现场复核与限载裁定为准；历史封控记录保留发布时的等级快照，不被后续裁定改写；
- 处置结论在同一事务边界内回写巡查台账、病害待办与养护工程待办，
  三处与总览看板读取同一个裁定版本号。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.seed import CORRIDOR_SEGMENTS
from app.store import store

STAGE_ORDER = ["待复核", "已复核", "已裁定", "已封控"]
GRADE_ORDER = ["轻微", "一般", "较重", "严重", "危急"]
REPORT_TYPES = {"disease_reported", "patrol_overdue", "load_limit_ruled"}
ACTIONS = ["现场复核", "等级裁定", "封控发布", "派单", "处置结论"]

SEED_TIME = "2026-09-30 08:00:00"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _grade_rank(grade: Any) -> int:
    return GRADE_ORDER.index(grade) if grade in GRADE_ORDER else -1


class RiskCorridorService:
    """风险走廊聚合根：段状态机、事件日志与增量投影都收在这一处。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._segments: list[dict[str, Any]] = [dict(seg) for seg in CORRIDOR_SEGMENTS]
        self._segments.sort(key=lambda seg: (int(seg.get("起点米", 0)), int(seg.get("终点米", 0))))
        self._events: list[dict[str, Any]] = []
        self._applied_event_ids: set[str] = set()
        self._cursor = 0
        self._reported_diseases: dict[int, set[str]] = {
            int(seg["id"]): set(seg.get("已上报病害", [])) for seg in self._segments
        }
        for seg in self._segments:
            seg["待处置病害数"] = len(self._reported_diseases[int(seg["id"])])
        self._projection: dict[str, Any] = {
            "stage_counts": {stage: 0 for stage in STAGE_ORDER},
            "overdue_segments": 0,
            "closures_total": 0,
            "dispatched_total": 0,
            "concluded_total": 0,
        }
        self._rebuild_projection()
        self._seed_genesis_events()

    # ------------------------------------------------------------------ 查询

    def list_segments(
        self,
        *,
        stage: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            segments = self._segments
            if stage:
                segments = [seg for seg in segments if seg.get("stage") == stage]
            if keyword:
                segments = [
                    seg
                    for seg in segments
                    if keyword in str(seg.get("路段名称", ""))
                    or keyword in str(seg.get("起点桩号", ""))
                    or keyword in str(seg.get("终点桩号", ""))
                ]
            return [self._view(seg) for seg in segments]

    def overview(self) -> dict[str, Any]:
        """总览看板：直接读增量投影，卡片与走廊段共用同一裁定版本。"""
        with self._lock:
            counts = self._projection["stage_counts"]
            cards = [
                {"label": "走廊段数", "value": len(self._segments)},
                {"label": "待复核", "value": counts["待复核"]},
                {"label": "待封控", "value": counts["已裁定"]},
                {"label": "封控中", "value": counts["已封控"]},
                {"label": "巡查逾期段", "value": self._projection["overdue_segments"]},
                {"label": "已下结论", "value": self._projection["concluded_total"]},
            ]
            return {
                "cards": cards,
                "cursor": self._cursor,
                "projection": {
                    "stage_counts": dict(counts),
                    "overdue_segments": self._projection["overdue_segments"],
                    "closures_total": self._projection["closures_total"],
                    "dispatched_total": self._projection["dispatched_total"],
                    "concluded_total": self._projection["concluded_total"],
                },
            }

    def events_after(self, after: int, limit: int) -> dict[str, Any]:
        """事件重放：从游标 after 之后继续，断线重连不丢不重。"""
        with self._lock:
            items = [event for event in self._events if int(event["cursor"]) > after]
            return {"items": items[:limit], "cursor": self._cursor, "total": len(items)}

    # ------------------------------------------------------------------ 动作

    def run_action(
        self, segment_id: int, action: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        if action == "现场复核":
            return self._review(segment_id, values)
        if action == "等级裁定":
            return self._rule(segment_id, values)
        if action == "封控发布":
            return self._close(segment_id, values)
        if action == "派单":
            return self._dispatch(segment_id, values)
        if action == "处置结论":
            return self._conclude(segment_id, values)
        return None, f"动作「{action}」不属于风险走廊可执行范围（{'、'.join(ACTIONS)}）"

    def report(
        self, event_id: str, event_type: str, segment_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """接收外部上报：病害、巡查逾期、限载裁定。按 event_id 幂等，重复上报不累加。"""
        with self._lock:
            if not event_id.strip():
                return None, "缺少 event_id，无法保证上报幂等"
            if event_type not in REPORT_TYPES:
                return None, f"事件类型「{event_type}」不支持，可选：{'、'.join(sorted(REPORT_TYPES))}"
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            error = self._validate_report(event_type, payload)
            if error:
                return None, error
            event, fresh = self._append_event(event_id.strip(), event_type, segment_id, dict(payload))
            if not fresh:
                return event, f"重复上报已忽略（幂等键 {event_id}），事件游标 {event['cursor']}"
            return event, f"上报已受理，事件游标 {event['cursor']}"

    # ------------------------------------------------------------------ 状态机

    def _review(
        self, segment_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            if segment["stage"] != "待复核":
                return None, f"当前阶段为「{segment['stage']}」，现场复核只能从「待复核」推进"
            grade = str(values.get("复核等级") or "").strip()
            if grade not in GRADE_ORDER:
                return None, f"复核等级「{grade}」不在等级序列（{'、'.join(GRADE_ORDER)}）内"
            reviewer = str(values.get("复核人") or "值班员").strip()
            event_id = str(values.get("event_id") or f"review-{segment_id}-{self._cursor + 1}")
            event, fresh = self._append_event(
                event_id,
                "field_reviewed",
                segment_id,
                {"复核等级": grade, "复核人": reviewer, "复核时间": _now()},
            )
            if not fresh:
                return self._view(segment), "重复的现场复核提交已被忽略（幂等）"
            return self._view(segment), f"现场复核完成，复核等级裁为 {grade}"

    def _rule(
        self, segment_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            if segment["stage"] == "待复核":
                return None, "未复核不得跳级裁定，请先完成现场复核"
            if segment["stage"] == "已封控":
                return None, "该段已发布封控，历史封控记录按原等级保留，不再重新裁定"
            grade = str(values.get("裁定等级") or "").strip()
            if grade not in GRADE_ORDER:
                return None, f"裁定等级「{grade}」不在等级序列（{'、'.join(GRADE_ORDER)}）内"
            load_limit = str(values.get("桥梁限载") or "").strip() or None
            event_id = str(values.get("event_id") or f"rule-{segment_id}-{self._cursor + 1}")
            payload: dict[str, Any] = {"裁定等级": grade, "裁定时间": _now()}
            if load_limit:
                payload["桥梁限载"] = load_limit
            event, fresh = self._append_event(event_id, "grade_ruled", segment_id, payload)
            if not fresh:
                return self._view(segment), "重复的等级裁定提交已被忽略（幂等）"
            return self._view(segment), f"等级裁定完成：{grade}，裁定版本 v{segment['ruling_version']}"

    def _close(
        self, segment_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            if segment["stage"] == "待复核":
                return None, "未复核不得跳级封控，请先完成现场复核"
            if segment["stage"] == "已复核":
                return None, "等级裁定未完成，不得发布封控"
            if segment["stage"] == "已封控":
                return None, "该段已发布封控，历史封控记录按原等级保留"
            measure = str(values.get("封控措施") or "").strip()
            if not measure:
                return None, "缺少封控措施，无法发布封控"
            event_id = str(values.get("event_id") or f"close-{segment_id}-{self._cursor + 1}")
            event, fresh = self._append_event(
                event_id, "closure_published", segment_id, {"封控措施": measure, "发布时间": _now()}
            )
            if not fresh:
                return self._view(segment), "重复的封控发布提交已被忽略（幂等）"
            return self._view(segment), f"封控已发布：{measure}（等级快照 {segment['closures'][-1]['封控等级']}）"

    def _dispatch(
        self, segment_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            if segment.get("dispatch"):
                return None, "该段已派单，重复派单不会累加"
            if segment.get("处置结论"):
                return None, "该段已下处置结论，派单入口已关闭"
            unit = str(values.get("处置单位") or "").strip()
            if not unit:
                return None, "缺少处置单位，无法派单"
            event_id = str(values.get("event_id") or f"dispatch-{segment_id}-{self._cursor + 1}")
            event, fresh = self._append_event(
                event_id, "dispatch_created", segment_id, {"处置单位": unit, "派单时间": _now()}
            )
            if not fresh:
                return self._view(segment), "重复的派单提交已被忽略（幂等）"
            return self._view(segment), f"已派单至 {unit}"

    def _conclude(
        self, segment_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            segment = self._segment(segment_id)
            if segment is None:
                return None, f"走廊段 {segment_id} 不存在"
            if segment.get("处置结论"):
                return None, "该段已下处置结论，重复结论不会累加"
            if not segment.get("closures") and not segment.get("dispatch"):
                return None, "尚未封控或派单，不能先下处置结论"
            conclusion = str(values.get("结论") or "").strip()
            if not conclusion:
                return None, "缺少处置结论内容"
            event_id = str(values.get("event_id") or f"conclude-{segment_id}-{self._cursor + 1}")
            event, fresh = self._append_event(
                event_id, "disposal_concluded", segment_id, {"结论": conclusion, "结论时间": _now()}
            )
            if not fresh:
                return self._view(segment), "重复的处置结论提交已被忽略（幂等）"
            return (
                self._view(segment),
                f"处置结论已回写巡查台账、病害待办与总览看板（裁定版本 v{segment['ruling_version']}），养护工程待办已重算",
            )

    # ------------------------------------------------------------------ 事件与投影

    def _append_event(
        self, event_id: str, event_type: str, segment_id: int, payload: dict[str, Any],
        *, apply: bool = True, occurred_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """写事件日志并就地投影。调用方必须先完成校验并持有锁：要么不写，要么写事件+投影一起生效。"""
        if event_id in self._applied_event_ids:
            for event in self._events:
                if event["event_id"] == event_id:
                    return event, False
        self._cursor += 1
        event = {
            "cursor": self._cursor,
            "event_id": event_id,
            "type": event_type,
            "segment_id": segment_id,
            "payload": payload,
            "occurred_at": occurred_at or _now(),
        }
        self._events.append(event)
        self._applied_event_ids.add(event_id)
        if apply:
            self._apply(event)
        return event, True

    def _apply(self, event: dict[str, Any]) -> None:
        segment = self._segment(int(event["segment_id"]))
        if segment is None:
            return
        event_type = event["type"]
        payload = event["payload"]
        if event_type == "disease_reported":
            code = str(payload.get("病害编号") or "").strip()
            diseases = self._reported_diseases.setdefault(int(segment["id"]), set())
            if code and code not in diseases:
                diseases.add(code)
                segment["待处置病害数"] = len(diseases)
            grade = payload.get("病害等级")
            if _grade_rank(grade) > _grade_rank(segment.get("上报病害等级")):
                segment["上报病害等级"] = grade
        elif event_type == "patrol_overdue":
            days = int(payload.get("逾期天数") or 0)
            if days > int(segment.get("巡查逾期天数") or 0):
                if not segment.get("巡查逾期天数"):
                    self._projection["overdue_segments"] += 1
                segment["巡查逾期天数"] = days
        elif event_type == "load_limit_ruled":
            # 限载裁定与现场复核同级优先：直接覆盖桥梁限载提示。
            segment["桥梁限载"] = payload.get("桥梁限载")
        elif event_type == "field_reviewed":
            segment["复核等级"] = payload.get("复核等级")
            segment["复核人"] = payload.get("复核人")
            segment["复核时间"] = payload.get("复核时间")
            self._move_stage(segment, "已复核")
        elif event_type == "grade_ruled":
            segment["裁定等级"] = payload.get("裁定等级")
            segment["裁定时间"] = payload.get("裁定时间")
            segment["ruling_version"] = int(segment.get("ruling_version", 0)) + 1
            if payload.get("桥梁限载"):
                segment["桥梁限载"] = payload["桥梁限载"]
            if segment["stage"] == "已复核":
                self._move_stage(segment, "已裁定")
        elif event_type == "closure_published":
            # 历史封控记录按发布时的等级快照保留，后续裁定不回写。
            segment["closures"].append({
                "封控等级": self._effective_grade(segment),
                "裁定版本": segment["ruling_version"],
                "封控措施": payload.get("封控措施"),
                "发布时间": payload.get("发布时间"),
            })
            self._projection["closures_total"] += 1
            self._move_stage(segment, "已封控")
        elif event_type == "dispatch_created":
            segment["dispatch"] = {"处置单位": payload.get("处置单位"), "派单时间": payload.get("派单时间")}
            self._projection["dispatched_total"] += 1
        elif event_type == "disposal_concluded":
            segment["处置结论"] = payload.get("结论")
            segment["结论时间"] = payload.get("结论时间")
            self._projection["concluded_total"] += 1
            self._write_back(segment)

    def _write_back(self, segment: dict[str, Any]) -> None:
        """处置结论回写：巡查台账、病害待办、养护工程待办，全部落在同一裁定版本上。"""
        version = int(segment.get("ruling_version", 0))
        conclusion = segment.get("处置结论")
        for patrol_id in segment.get("patrol_ids", []):
            row = store.find("patrol", int(patrol_id))
            if row is not None:
                row["处置措施"] = conclusion
                row["status"] = "已复核"
                row["pending"] = False
                row["裁定版本"] = version
        for disease_id in segment.get("disease_ids", []):
            row = store.find("pavement", int(disease_id))
            if row is not None:
                row["status"] = "已修复"
                row["病害状态"] = "已修复"
                row["pending"] = False
                row["裁定版本"] = version
        for project_id in segment.get("project_ids", []):
            self._recalc_project_todo(int(project_id))

    def _recalc_project_todo(self, project_id: int) -> None:
        """养护工程待办重算：只要还有关联走廊段未下结论，工程待办保持打开。"""
        row = store.find("project", project_id)
        if row is None:
            return
        open_segments = [
            seg
            for seg in self._segments
            if project_id in seg.get("project_ids", []) and not seg.get("处置结论")
        ]
        row["pending"] = bool(open_segments)
        row["待办重算时间"] = _now()

    # ------------------------------------------------------------------ 内部工具

    def _segment(self, segment_id: int) -> dict[str, Any] | None:
        for seg in self._segments:
            if int(seg.get("id", 0)) == int(segment_id):
                return seg
        return None

    def _move_stage(self, segment: dict[str, Any], target: str) -> None:
        current = segment["stage"]
        if current == target:
            return
        self._projection["stage_counts"][current] -= 1
        self._projection["stage_counts"][target] += 1
        segment["stage"] = target

    def _effective_grade(self, segment: dict[str, Any]) -> str:
        """等级冲突时以现场复核与限载裁定为准：裁定 > 复核 > 上报。"""
        return (
            segment.get("裁定等级")
            or segment.get("复核等级")
            or segment.get("上报病害等级")
            or "一般"
        )

    def _available_actions(self, segment: dict[str, Any]) -> list[str]:
        stage = segment["stage"]
        actions: list[str] = []
        if stage == "待复核":
            actions.append("现场复核")
        if stage in ("已复核", "已裁定"):
            actions.append("等级裁定")
        if stage == "已裁定":
            actions.append("封控发布")
        if not segment.get("dispatch") and not segment.get("处置结论"):
            actions.append("派单")
        if (segment.get("closures") or segment.get("dispatch")) and not segment.get("处置结论"):
            actions.append("处置结论")
        return actions

    def _view(self, segment: dict[str, Any]) -> dict[str, Any]:
        view = dict(segment)
        view["有效等级"] = self._effective_grade(segment)
        view["阶段序号"] = STAGE_ORDER.index(segment["stage"])
        view["可执行动作"] = self._available_actions(segment)
        return view

    def _validate_report(self, event_type: str, payload: dict[str, Any]) -> str | None:
        if event_type == "disease_reported":
            if not str(payload.get("病害编号") or "").strip():
                return "病害上报缺少病害编号，无法按业务键去重"
            if payload.get("病害等级") not in GRADE_ORDER:
                return f"病害等级不在等级序列（{'、'.join(GRADE_ORDER)}）内"
        elif event_type == "patrol_overdue":
            try:
                if int(payload.get("逾期天数")) < 0:
                    return "逾期天数不能为负"
            except (TypeError, ValueError):
                return "逾期天数必须是数字"
        elif event_type == "load_limit_ruled":
            if not str(payload.get("桥梁限载") or "").strip():
                return "限载裁定缺少桥梁限载内容"
        return None

    def _rebuild_projection(self) -> None:
        """启动时按种子段重建投影；运行期只走 _apply 的增量更新。"""
        for seg in self._segments:
            self._projection["stage_counts"][seg["stage"]] += 1
            if int(seg.get("巡查逾期天数") or 0) > 0:
                self._projection["overdue_segments"] += 1
            self._projection["closures_total"] += len(seg.get("closures", []))
            if seg.get("dispatch"):
                self._projection["dispatched_total"] += 1
            if seg.get("处置结论"):
                self._projection["concluded_total"] += 1

    def _seed_genesis_events(self) -> None:
        """把种子段的既有状态补记为历史事件（不再二次投影），让游标重放能还原全过程。"""
        for seg in self._segments:
            sid = int(seg["id"])
            n = 0

            def genesis(event_type: str, payload: dict[str, Any]) -> None:
                nonlocal n
                n += 1
                self._append_event(
                    f"seed-{sid}-{n}", event_type, sid, payload, apply=False, occurred_at=SEED_TIME
                )

            for code in seg.get("已上报病害", []):
                genesis("disease_reported", {"病害编号": code, "病害等级": seg.get("上报病害等级")})
            if int(seg.get("巡查逾期天数") or 0) > 0:
                genesis("patrol_overdue", {"逾期天数": seg["巡查逾期天数"]})
            if STAGE_ORDER.index(seg["stage"]) >= STAGE_ORDER.index("已复核"):
                genesis("field_reviewed", {
                    "复核等级": seg.get("复核等级"),
                    "复核人": seg.get("复核人"),
                    "复核时间": seg.get("复核时间"),
                })
            if seg.get("桥梁限载"):
                genesis("load_limit_ruled", {"桥梁限载": seg["桥梁限载"]})
            if STAGE_ORDER.index(seg["stage"]) >= STAGE_ORDER.index("已裁定"):
                genesis("grade_ruled", {
                    "裁定等级": seg.get("裁定等级"),
                    "裁定时间": seg.get("裁定时间"),
                    **({"桥梁限载": seg["桥梁限载"]} if seg.get("桥梁限载") else {}),
                })
            for closure in seg.get("closures", []):
                genesis("closure_published", {
                    "封控措施": closure.get("封控措施"),
                    "发布时间": closure.get("发布时间"),
                })


risk_corridor_service = RiskCorridorService()
