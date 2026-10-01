"""风险走廊业务规则：单向推进、裁定版本、增量投影与回写同步都收在这里。

设计要点：
- 阶段只许「待复核 → 已复核 → 已裁定 → 已封控」单向推进，未复核不得跳级封控；
- 等级冲突时以现场复核和限载裁定为准，历史封控记录按原等级保留；
- 每次推进追加事件（游标单调递增）、按上报编号幂等去重，重复上报不累加；
- 聚合投影随业务写在同一事务内落库，巡查台账、病害清单、总览看板读同一裁定版本。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

SEGMENT_MODULE = "risk_segment"
EVENT_MODULE = "risk_event"
CLOSURE_MODULE = "risk_closure"
REPORT_MODULE = "risk_report"
PROJECTION_MODULE = "risk_projection"

STAGE_ORDER = ["待复核", "已复核", "已裁定", "已封控"]
LEVEL_ORDER = ["一般", "较大", "重大"]
ACTIONS = ("现场复核", "等级裁定", "封控发布", "派单处置")
# 桥梁限载时裁定等级的下限：限载裁定优先于上报与复核结论。
LIMIT_FLOOR_LEVEL = "较大"
GLOBAL_PROJECTION_ID = 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mileage(stake: str) -> int:
    """把 K12+800 换算成米，用于走廊段按里程排序。"""
    text = str(stake or "").strip().upper().lstrip("K")
    try:
        km, _, meter = text.partition("+")
        return int(km) * 1000 + int(meter)
    except ValueError:
        return 0


def _level_rank(level: str | None) -> int:
    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else -1


class RiskCorridorService:
    def __init__(self) -> None:
        self._rebuild_projection()

    # ---------- 查询 ----------

    def list_segments(self) -> list[dict[str, Any]]:
        """按里程展开走廊段，每段带出待处置病害等级、巡查逾期与桥梁限载提示。"""
        views = [self._segment_view(row) for row in store.rows(SEGMENT_MODULE)]
        views.sort(key=lambda item: (item["路段名称"], _mileage(item["起点桩号"])))
        return views

    def summary(self) -> dict[str, Any]:
        """总览看板：直接读聚合投影，时间相关的逾期/限载按当天口径折算。"""
        projection = self._global_projection()
        views = self.list_segments()
        cards = [
            {"label": "走廊段数", "value": len(views)},
            {"label": "待复核", "value": projection["待复核"]},
            {"label": "已裁定待封控", "value": projection["已裁定"]},
            {"label": "封控中", "value": projection["封控中"]},
            {"label": "巡查逾期段", "value": sum(1 for view in views if view["巡查逾期"])},
            {"label": "限载桥梁段", "value": sum(1 for view in views if view["限载"])},
            {"label": "养护工程待办", "value": projection["养护工程待办"]},
        ]
        return {"cards": cards, "最新事件游标": projection["最新事件游标"]}

    def list_events(self, *, after: int = 0, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        """事件重放：只返回游标之后的事件，断线后客户端带着旧游标继续。"""
        events = sorted(store.rows(EVENT_MODULE), key=lambda row: int(row["cursor"]))
        latest = events[-1]["cursor"] if events else 0
        return [row for row in events if int(row["cursor"]) > after][:limit], latest

    def list_closures(self) -> list[dict[str, Any]]:
        """历史封控记录按发布时的原等级保留，只读不回写。"""
        return sorted(store.rows(CLOSURE_MODULE), key=lambda row: int(row["id"]), reverse=True)

    # ---------- 动作 ----------

    def run_action(self, segment_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        segment = store.find(SEGMENT_MODULE, segment_id)
        if segment is None:
            return None, f"风险走廊段 {segment_id} 不存在或已归档"
        action = str(values.get("action") or "").strip()
        if action not in ACTIONS:
            return None, f"动作「{action}」不属于风险走廊可执行范围"
        report_key = str(values.get("上报编号") or "").strip()
        if not report_key:
            return None, "缺少必填字段：上报编号（用于重复上报去重）"
        if self._find_report(report_key) is not None:
            return self._segment_view(segment), f"重复上报已忽略（上报编号 {report_key}），聚合数据未重复累加"
        stage_error = self._check_stage(action, str(segment["阶段"]))
        if stage_error:
            return None, stage_error

        if action == "现场复核":
            return self._review(segment, report_key, values)
        if action == "等级裁定":
            return self._rule(segment, report_key)
        if action == "封控发布":
            return self._close(segment, report_key)
        return self._dispatch(segment, report_key, values)

    # ---------- 各阶段处理：校验在锁外完成，落库在同一事务内 ----------

    def _review(self, segment: dict[str, Any], report_key: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        level = str(values.get("复核等级") or "").strip()
        if level not in LEVEL_ORDER:
            return None, f"复核等级需为：{'、'.join(LEVEL_ORDER)}"
        reviewer = str(values.get("复核人") or "").strip() or "值班复核员"
        conflict = level != str(segment["上报等级"])
        message = f"现场复核完成：复核等级{level}"
        if conflict:
            message += f"，与上报等级{segment['上报等级']}冲突，裁定时以现场复核为准"
        with store.transaction():
            segment["复核等级"] = level
            segment["复核人"] = reviewer
            segment["复核时间"] = _now()
            self._set_stage(segment, "已复核")
            self._append_event("现场复核", segment, report_key, message, ["总览看板"])
            self._apply_delta({"待复核": -1, "已复核": 1})
        return self._segment_view(segment), message

    def _rule(self, segment: dict[str, Any], report_key: str) -> tuple[dict[str, Any] | None, str]:
        reviewed = str(segment["复核等级"])
        reported = str(segment["上报等级"])
        limited, bridge_label = self._bridge_limit(segment)
        ruled, source = reviewed, "现场复核"
        if limited and _level_rank(ruled) < _level_rank(LIMIT_FLOOR_LEVEL):
            ruled, source = LIMIT_FLOOR_LEVEL, "限载裁定"
        version = int(segment["裁定版本"]) + 1
        message = f"等级裁定完成：裁定等级{ruled}（以{source}为准），裁定版本V{version}"
        if reported != reviewed:
            message += f"；上报{reported}与复核{reviewed}冲突，以现场复核为准"
        if limited:
            message += f"；{bridge_label}，限载提示已同步"
        with store.transaction():
            segment["裁定等级"] = ruled
            segment["等级来源"] = source
            segment["裁定版本"] = version
            segment["裁定时间"] = _now()
            self._set_stage(segment, "已裁定")
            self._sync_linked_rows(segment, f"等级裁定V{version}：{ruled}")
            self._append_event("等级裁定", segment, report_key, message, ["巡查台账", "病害清单", "总览看板"])
            self._apply_delta({"已复核": -1, "已裁定": 1})
        return self._segment_view(segment), message

    def _close(self, segment: dict[str, Any], report_key: str) -> tuple[dict[str, Any] | None, str]:
        version = int(segment["裁定版本"])
        level = str(segment["裁定等级"])
        closure_no = f"FK-{date.today():%Y}-{store.next_id(CLOSURE_MODULE):04d}"
        message = (
            f"封控发布完成：{closure_no}，封控等级{level}（裁定版本V{version}），"
            "结论已同步巡查台账、病害清单与总览看板"
        )
        with store.transaction():
            store.rows(CLOSURE_MODULE).append({
                "id": store.next_id(CLOSURE_MODULE),
                "封控编号": closure_no,
                "走廊段编号": segment["走廊段编号"],
                "桩号区间": f"{segment['起点桩号']}~{segment['终点桩号']}",
                "封控等级": level,  # 历史封控按原等级保留，后续裁定不回写
                "裁定版本": version,
                "发布时间": _now(),
                "状态": "生效中",
                "结论": f"按裁定等级{level}发布封控",
            })
            self._set_stage(segment, "已封控")
            segment["pending"] = False
            self._sync_linked_rows(segment, f"封控发布{closure_no}（{level}，裁定版本V{version}）")
            self._append_event("封控发布", segment, report_key, message, ["巡查台账", "病害清单", "总览看板"])
            self._apply_delta({"已裁定": -1, "封控中": 1})
        return self._segment_view(segment), message

    def _dispatch(self, segment: dict[str, Any], report_key: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        version = int(segment["裁定版本"])
        contractor = str(values.get("承建单位") or "").strip() or "市养护工程中心"
        project_no = f"PROJ-{store.next_id('project'):04d}"
        message = (
            f"派单处置完成：生成养护工程{project_no}（裁定版本V{version}），"
            "结论已同步巡查台账、病害清单与总览看板，养护工程待办已重算"
        )
        with store.transaction():
            store.rows("project").append({
                "id": store.next_id("project"),
                "status": "待开工",
                "pending": True,
                "abnormal": False,
                "工程编号": project_no,
                "工程名称": f"{segment['路段名称']}{segment['起点桩号']}~{segment['终点桩号']}应急处治工程",
                "工程类型": "应急处治",
                "施工路段": segment["路段名称"],
                "承建单位": contractor,
                "开工日期": str(date.today()),
                "竣工日期": "",
                "工程状态": "待开工",
                "走廊段编号": segment["走廊段编号"],
                "裁定版本": version,
            })
            self._sync_linked_rows(segment, f"派单处置{project_no}（裁定版本V{version}）", dispatch=True)
            self._append_event("派单处置", segment, report_key, message, ["巡查台账", "病害清单", "总览看板", "养护工程待办"])
            self._apply_delta({"派单处置": 1})
            self._recalc_project_todo()
        return self._segment_view(segment), message

    # ---------- 内部规则 ----------

    def _check_stage(self, action: str, stage: str) -> str | None:
        """单向推进校验：任何跳级、回退都在这里被拦下。"""
        if action == "现场复核" and stage != "待复核":
            return f"风险走廊按现场复核、等级裁定、封控发布单向推进，当前阶段「{stage}」不可回退复核"
        if action == "等级裁定" and stage != "已复核":
            if stage == "待复核":
                return "未复核不得跳级裁定：请先完成现场复核"
            return f"当前阶段「{stage}」已完成裁定，不可重复裁定"
        if action == "封控发布":
            if stage == "待复核":
                return "未复核不得跳级封控：请先完成现场复核与等级裁定"
            if stage == "已复核":
                return "未裁定不得封控：请先完成等级裁定"
            if stage == "已封控":
                return "该走廊段已发布封控，历史封控记录按原等级保留"
        if action == "派单处置" and stage != "已裁定":
            if stage == "待复核":
                return "未复核不得跳级派单：请先完成现场复核与等级裁定"
            if stage == "已复核":
                return "未裁定不得派单：请先完成等级裁定"
            if stage == "已封控":
                return "该走廊段已封控，处置进展请在封控记录内跟踪"
        return None

    def _set_stage(self, segment: dict[str, Any], stage: str) -> None:
        segment["阶段"] = stage
        segment["status"] = stage
        segment["pending"] = stage != STAGE_ORDER[-1]

    def _sync_linked_rows(self, segment: dict[str, Any], conclusion: str, *, dispatch: bool = False) -> None:
        """处置结论回写巡查台账与病害清单，两处与看板读同一裁定版本。"""
        code = segment["走廊段编号"]
        version = int(segment["裁定版本"])
        for patrol in store.rows("patrol"):
            if patrol.get("走廊段编号") == code:
                patrol["处置措施"] = conclusion
                patrol["裁定版本"] = version
                patrol["status"] = "已复核"
                patrol["巡查状态"] = "已复核"
                patrol["pending"] = False
        for disease in store.rows("pavement"):
            if disease.get("走廊段编号") == code and disease.get("pending"):
                disease["处置结论"] = conclusion
                disease["裁定版本"] = version
                if dispatch:
                    disease["status"] = "修复中"
                    disease["病害状态"] = "修复中"

    def _append_event(self, action: str, segment: dict[str, Any], report_key: str, conclusion: str, targets: list[str]) -> None:
        events = store.rows(EVENT_MODULE)
        cursor = max((int(row["cursor"]) for row in events), default=0) + 1
        events.append({
            "cursor": cursor,
            "类型": action,
            "走廊段编号": segment["走廊段编号"],
            "桩号区间": f"{segment['起点桩号']}~{segment['终点桩号']}",
            "裁定版本": int(segment["裁定版本"]),
            "结论": conclusion,
            "上报编号": report_key,
            "时间": _now(),
            "同步目标": "、".join(targets),
        })
        store.rows(REPORT_MODULE).append({"上报编号": report_key, "cursor": cursor, "类型": action})
        self._apply_delta({"事件总数": 1, "最新事件游标": cursor}, absolute={"最新事件游标"})

    def _find_report(self, report_key: str) -> dict[str, Any] | None:
        for row in store.rows(REPORT_MODULE):
            if row.get("上报编号") == report_key:
                return row
        return None

    # ---------- 聚合投影 ----------

    def _rebuild_projection(self) -> None:
        """启动时全量建一次投影，之后只靠事件增量推进。"""
        rows = store.rows(PROJECTION_MODULE)
        rows.clear()
        stages = {stage: 0 for stage in STAGE_ORDER}
        for segment in store.rows(SEGMENT_MODULE):
            stages[str(segment["阶段"])] = stages.get(str(segment["阶段"]), 0) + 1
        rows.append({
            "id": GLOBAL_PROJECTION_ID,
            "待复核": stages["待复核"],
            "已复核": stages["已复核"],
            "已裁定": stages["已裁定"],
            "封控中": stages["已封控"],
            "派单处置": 0,
            "事件总数": 0,
            "最新事件游标": 0,
            "养护工程待办": sum(1 for row in store.rows("project") if row.get("pending")),
        })

    def _global_projection(self) -> dict[str, Any]:
        projection = store.find(PROJECTION_MODULE, GLOBAL_PROJECTION_ID)
        if projection is None:
            self._rebuild_projection()
            projection = store.find(PROJECTION_MODULE, GLOBAL_PROJECTION_ID)
        assert projection is not None
        return projection

    def _apply_delta(self, delta: dict[str, int], *, absolute: set[str] | None = None) -> None:
        """增量投影：只加不减地按事件应用差量，调用方必须处在事务内。"""
        projection = self._global_projection()
        for key, value in delta.items():
            if absolute and key in absolute:
                projection[key] = value
            else:
                projection[key] = int(projection.get(key, 0)) + value

    def _recalc_project_todo(self) -> None:
        """养护工程待办重算：以工程台账为准全量重算，而不是简单 +1。"""
        self._global_projection()["养护工程待办"] = sum(
            1 for row in store.rows("project") if row.get("pending")
        )

    # ---------- 视图组装 ----------

    def _bridge_limit(self, segment: dict[str, Any]) -> tuple[bool, str]:
        bridge_code = segment.get("关联桥梁编号")
        if not bridge_code:
            return False, ""
        for bridge in store.rows("bridge_info"):
            if bridge.get("桥梁编号") == bridge_code:
                if bridge.get("status") == "限载":
                    tonnage = str(bridge.get("限载吨位") or "").strip()
                    label = f"{bridge['桥梁名称']}限载{tonnage or '通行'}"
                    return True, label
                return False, ""
        return False, ""

    def _segment_view(self, segment: dict[str, Any]) -> dict[str, Any]:
        code = segment["走廊段编号"]
        diseases = [
            row for row in store.rows("pavement")
            if row.get("走廊段编号") == code and row.get("pending")
        ]
        top_level = max((_level_rank(str(row.get("严重程度"))) for row in diseases), default=-1)
        due = date.fromisoformat(str(segment["巡查到期日"]))
        overdue_days = (date.today() - due).days
        limited, limit_label = self._bridge_limit(segment)
        stage = str(segment["阶段"])
        next_action = {
            "待复核": "现场复核",
            "已复核": "等级裁定",
            "已裁定": "封控发布 / 派单处置",
            "已封控": "—",
        }[stage]
        return {
            "id": segment["id"],
            "走廊段编号": code,
            "路段名称": segment["路段名称"],
            "起点桩号": segment["起点桩号"],
            "终点桩号": segment["终点桩号"],
            "桩号区间": f"{segment['起点桩号']}~{segment['终点桩号']}",
            "上报等级": segment["上报等级"],
            "复核等级": segment.get("复核等级") or "—",
            "裁定等级": segment.get("裁定等级") or "—",
            "等级来源": segment.get("等级来源") or "—",
            "裁定版本": int(segment["裁定版本"]),
            "阶段": stage,
            "下一动作": next_action,
            "待处置病害数": len(diseases),
            "待处置病害等级": LEVEL_ORDER[top_level] if top_level >= 0 else "—",
            "巡查到期日": segment["巡查到期日"],
            "巡查逾期": overdue_days > 0,
            "巡查逾期天数": max(overdue_days, 0),
            "限载": limited,
            "桥梁限载提示": limit_label if limited else "—",
            "等级冲突": bool(segment.get("复核等级")) and segment.get("复核等级") != segment.get("上报等级"),
        }
