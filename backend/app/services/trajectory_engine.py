"""供应链轨迹核算引擎（纯函数、仅依赖标准库）。

输入订单环节事件（开始/结束，含尝试编号、业务时间、父环节），输出：

1. 配对后的作业段（segment），保留配对依据与无法确定的候选；
2. 订单级汇总：处理时间（区间并集）、等待时间、未完成段、返工、相互覆盖的作业段；
3. 瓶颈数据集：基于反事实区间并集量化单环节改善可减少的订单时长，
   并行重叠自动抵扣，而不是把各环节耗时直接相加；
4. 版本指纹、父子成环、时区不一致、结束早于开始、缺事件、重复事件等审计信息。

所有耗时以秒为单位；无法成立的耗时一律给 ``None`` 并附原因，绝不补零。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

START = "start"
END = "end"

# 段状态
SEG_COMPLETED = "completed"
SEG_OPEN = "open"
SEG_INVALID = "invalid_end_before_start"

# 配对可信度
CONF_CONFIRMED = "confirmed"
CONF_AMBIGUOUS = "ambiguous"

# 问题级别
ISSUE_ERROR = "error"
ISSUE_WARNING = "warning"


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def parse_business_time(raw: Any) -> tuple[datetime | None, str | None, str | None]:
    """把业务时间解析为 UTC 感知时间。

    返回 ``(utc_datetime, 时区标签, 错误原因)``。 naive 时间按 UTC 处理并标注；
    无法解析时第一个返回值为 ``None``，调用方须把事件放入未配对候选而不是丢弃。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None, "业务时间为空或不是字符串"
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None, f"业务时间无法解析：{text}"
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc), "naive(按UTC处理)", None
    offset = dt.utcoffset() or timedelta(0)
    label = f"UTC{_format_offset(offset)}"
    return dt.astimezone(timezone.utc), label, None


def _format_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# --------------------------------------------------------------------------- #
# 事件规范化
# --------------------------------------------------------------------------- #
@dataclass
class NormalizedEvent:
    event_id: str
    order_id: str
    stage: str
    attempt: int
    event_type: str
    time: datetime
    offset_label: str | None
    parent_stage: str | None
    reported_at: datetime | None
    raw_time: str
    seq: int
    duplicate_of: str | None = None
    parse_error: str | None = None


def normalize_event(raw: dict[str, Any], seq: int) -> tuple[NormalizedEvent | None, list[dict[str, str]]]:
    """单条事件规范化；结构性错误随事件保留，时间错误挂在事件上进入未配对候选。"""
    issues: list[dict[str, str]] = []
    order_id = str(raw.get("order_id") or "").strip()
    stage = str(raw.get("stage") or "").strip()
    event_type = str(raw.get("event_type") or "").strip().lower()
    if not order_id:
        issues.append({"level": ISSUE_ERROR, "code": "missing_order_id", "message": "事件缺少订单编号"})
    if not stage:
        issues.append({"level": ISSUE_ERROR, "code": "missing_stage", "message": "事件缺少环节名称"})
    if event_type not in (START, END):
        issues.append({"level": ISSUE_ERROR, "code": "bad_event_type",
                       "message": f"事件类型必须是 start/end，收到：{raw.get('event_type')!r}"})
    if issues:
        return None, issues

    try:
        attempt = int(raw.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
        issues.append({"level": ISSUE_WARNING, "code": "bad_attempt",
                       "message": f"尝试编号无法识别，按第 1 次处理：{raw.get('attempt')!r}"})
    if attempt < 1:
        issues.append({"level": ISSUE_WARNING, "code": "bad_attempt",
                       "message": f"尝试编号小于 1，按第 1 次处理：{attempt}"})
        attempt = 1

    event_id = str(raw.get("event_id") or f"evt-{uuid4().hex[:10]}").strip()
    parent = raw.get("parent_stage")
    parent_stage = str(parent).strip() if parent not in (None, "") else None

    reported_dt = None
    reported_raw = raw.get("reported_at")
    if reported_raw:
        reported_dt, _, rep_err = parse_business_time(reported_raw)
        if rep_err:
            issues.append({"level": ISSUE_WARNING, "code": "bad_reported_at",
                           "message": f"上报时间无法解析，已忽略上报序：{rep_err}"})

    utc_dt, offset_label, time_err = parse_business_time(raw.get("business_time"))
    event = NormalizedEvent(
        event_id=event_id,
        order_id=order_id,
        stage=stage,
        attempt=attempt,
        event_type=event_type,
        time=utc_dt or datetime.min.replace(tzinfo=timezone.utc),
        offset_label=offset_label,
        parent_stage=parent_stage,
        reported_at=reported_dt,
        raw_time=str(raw.get("business_time") or ""),
        seq=seq,
        parse_error=time_err,
    )
    if time_err:
        issues.append({"level": ISSUE_ERROR, "code": "bad_business_time",
                       "message": f"事件 {event_id}：{time_err}"})
    return event, issues


def fingerprint(events: list[NormalizedEvent]) -> str:
    """事件集合指纹：补报导致配对变化后，版本指纹必然不同。"""
    parts = sorted(
        f"{e.event_id}|{e.stage}|{e.attempt}|{e.event_type}|{iso(e.time)}|{e.raw_time}"
        for e in events
        if not e.duplicate_of and not e.parse_error
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 父子关系成环
# --------------------------------------------------------------------------- #
def detect_parent_cycle(events: list[NormalizedEvent]) -> list[str]:
    """返回成环环节清单（DFS 上色）；无环返回空列表。"""
    parents: dict[str, set[str]] = {}
    for ev in events:
        if ev.parent_stage:
            parents.setdefault(ev.stage, set()).add(ev.parent_stage)
            parents.setdefault(ev.parent_stage, set())
    color: dict[str, int] = {}  # 0 未访问 1 在栈中 2 已完成
    cyclic: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(parents.get(node, ())):
            state = color.get(nxt, 0)
            if state == 1:
                idx = stack.index(nxt)
                cyclic.update(stack[idx:])
            elif state == 0:
                visit(nxt, stack)
        stack.pop()
        color[node] = 2

    for stage in sorted(parents):
        if color.get(stage, 0) == 0:
            visit(stage, [])
    return sorted(cyclic)


# --------------------------------------------------------------------------- #
# 区间工具
# --------------------------------------------------------------------------- #
def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并重叠/相邻区间，返回有序并集。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[list[datetime]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    return int(sum((e - s).total_seconds() for s, e in merge_intervals(intervals)))


def diff_seconds(a: datetime, b: datetime) -> int:
    return int((a - b).total_seconds())


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #
def _dedupe(events: list[NormalizedEvent]) -> tuple[list[NormalizedEvent], list[dict[str, str]]]:
    """重复事件不重复占用时间：同 event_id 或同业务含义的事件只保留第一条。"""
    seen_ids: set[str] = set()
    seen_business: set[tuple[str, int, str, str]] = set()
    kept: list[NormalizedEvent] = []
    issues: list[dict[str, str]] = []
    for ev in events:
        if ev.event_id in seen_ids:
            issues.append({"level": ISSUE_WARNING, "code": "duplicate_event_id",
                           "message": f"事件 {ev.event_id} 重复上报，已去重，不重复占用时间"})
            continue
        business_key = (ev.stage, ev.attempt, ev.event_type, iso(ev.time) or ev.raw_time)
        if business_key in seen_business:
            issues.append({"level": ISSUE_WARNING, "code": "duplicate_business_event",
                           "message": (f"事件 {ev.event_id} 与同环节/尝试/类型/业务时间的事件重复，"
                                       "按重复上报去重，不重复占用时间")})
            continue
        seen_ids.add(ev.event_id)
        seen_business.add(business_key)
        kept.append(ev)
    return kept, issues


def pair_order(events: list[NormalizedEvent]) -> dict[str, Any]:
    """对单个订单的事件做栈式 LIFO 配对，支持乱序结束、并行与返工。

    配对依据完整保留：每个段记录开始/结束事件 id、配对方式、置信度；
    一个结束到达时若栈中存在多个未闭合开始，则标记 ambiguous 并附全部候选。
    """
    usable = [ev for ev in events if not ev.parse_error]
    bad_time = [ev for ev in events if ev.parse_error]
    segments: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []

    for ev in bad_time:
        unpaired.append({
            "event_id": ev.event_id, "stage": ev.stage, "attempt": ev.attempt,
            "event_type": ev.event_type, "business_time": ev.raw_time,
            "reason": ev.parse_error,
        })

    groups: dict[tuple[str, int], list[NormalizedEvent]] = {}
    for ev in usable:
        groups.setdefault((ev.stage, ev.attempt), []).append(ev)

    for (stage, attempt), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        ordered = sorted(group, key=lambda e: (e.time, 0 if e.event_type == START else 1, e.seq))
        stack: list[NormalizedEvent] = []
        consumed: set[str] = set()
        for ev in ordered:
            if ev.event_id in consumed:
                continue
            if ev.event_type == START:
                stack.append(ev)
                continue
            if not stack:
                # 结束事件早于组内任何开始：与最近的未来开始配成“结束早于开始”无效段，
                # 而不是草率归为缺开始；没有未来开始时才是真正的缺一侧事件。
                future_starts = [
                    e for e in ordered
                    if e.event_type == START and e.time > ev.time and e.event_id not in consumed
                ]
                if future_starts:
                    start_ev = min(future_starts, key=lambda e: e.time)
                    consumed.add(start_ev.event_id)
                    raw_delta = diff_seconds(ev.time, start_ev.time)
                    confidence = CONF_CONFIRMED if len(future_starts) == 1 else CONF_AMBIGUOUS
                    message = (f"环节 {stage} 第 {attempt} 次尝试结束时间早于开始时间"
                               f"（{ev.event_id} vs {start_ev.event_id}，原始差值 {raw_delta}s），"
                               "该段不计入耗时且不补零")
                    issues.append({"level": ISSUE_ERROR, "code": "end_before_start", "message": message})
                    segments.append({
                        "segment_id": f"seg-{uuid4().hex[:10]}",
                        "order_id": ev.order_id,
                        "stage": stage,
                        "attempt": attempt,
                        "status": SEG_INVALID,
                        "start_event_id": start_ev.event_id,
                        "end_event_id": ev.event_id,
                        "start_time": iso(start_ev.time),
                        "end_time": iso(ev.time),
                        "start_time_original": start_ev.raw_time,
                        "end_time_original": ev.raw_time,
                        "duration_seconds": None,
                        "raw_delta_seconds": raw_delta,
                        "pairing": {
                            "method": "nearest-future-start",
                            "confidence": confidence,
                            "candidate_start_event_ids": sorted(e.event_id for e in future_starts),
                            "basis": "结束早于组内全部开始，取最近的未来开始配对为无效段，耗时留空不补零",
                        },
                        "reported_at_start": iso(start_ev.reported_at),
                        "reported_at_end": iso(ev.reported_at),
                        "issues": [message],
                    })
                    continue
                unpaired.append({
                    "event_id": ev.event_id, "stage": stage, "attempt": attempt,
                    "event_type": END, "business_time": iso(ev.time),
                    "reason": "缺少开始事件，结束事件无法配对",
                })
                issues.append({"level": ISSUE_ERROR, "code": "missing_start",
                               "message": f"环节 {stage} 第 {attempt} 次尝试的结束事件 {ev.event_id} 缺少开始事件"})
                continue
            start_ev = stack.pop()
            candidates = [s.event_id for s in stack] + [start_ev.event_id]
            confidence = CONF_CONFIRMED if len(stack) == 0 else CONF_AMBIGUOUS
            raw_delta = diff_seconds(ev.time, start_ev.time)
            segment_issues: list[str] = []
            if raw_delta < 0:
                status = SEG_INVALID
                duration: int | None = None
                message = (f"环节 {stage} 第 {attempt} 次尝试结束时间早于开始时间"
                           f"（{ev.event_id} vs {start_ev.event_id}，原始差值 {raw_delta}s），"
                           "该段不计入耗时且不补零")
                issues.append({"level": ISSUE_ERROR, "code": "end_before_start", "message": message})
                segment_issues.append(message)
            else:
                status = SEG_COMPLETED
                duration = raw_delta
            segments.append({
                "segment_id": f"seg-{uuid4().hex[:10]}",
                "order_id": ev.order_id,
                "stage": stage,
                "attempt": attempt,
                "status": status,
                "start_event_id": start_ev.event_id,
                "end_event_id": ev.event_id,
                "start_time": iso(start_ev.time),
                "end_time": iso(ev.time),
                "start_time_original": start_ev.raw_time,
                "end_time_original": ev.raw_time,
                "duration_seconds": duration,
                "raw_delta_seconds": raw_delta,
                "pairing": {
                    "method": "stack-lifo",
                    "confidence": confidence,
                    "candidate_start_event_ids": sorted(candidates),
                    "basis": "按业务时间排序后栈式配对；乱序上报不影响业务时间语义",
                },
                "reported_at_start": iso(start_ev.reported_at),
                "reported_at_end": iso(ev.reported_at),
                "issues": segment_issues,
            })
        # 栈中剩余开始：未完成段，绝不当作零耗时
        for start_ev in reversed(stack):
            segments.append({
                "segment_id": f"seg-{uuid4().hex[:10]}",
                "order_id": start_ev.order_id,
                "stage": stage,
                "attempt": attempt,
                "status": SEG_OPEN,
                "start_event_id": start_ev.event_id,
                "end_event_id": None,
                "start_time": iso(start_ev.time),
                "end_time": None,
                "start_time_original": start_ev.raw_time,
                "end_time_original": None,
                "duration_seconds": None,
                "raw_delta_seconds": None,
                "pairing": {
                    "method": "stack-lifo",
                    "confidence": CONF_AMBIGUOUS,
                    "candidate_start_event_ids": [s.event_id for s in stack],
                    "basis": "缺少结束事件，保留为未完成段，仅给观测时长不闭合",
                },
                "reported_at_start": iso(start_ev.reported_at),
                "reported_at_end": None,
                "issues": [],
            })
            unpaired.append({
                "event_id": start_ev.event_id, "stage": stage, "attempt": attempt,
                "event_type": START, "business_time": iso(start_ev.time),
                "reason": "缺少结束事件，作业段尚未完成",
            })
            issues.append({"level": ISSUE_WARNING, "code": "missing_end",
                           "message": f"环节 {stage} 第 {attempt} 次尝试缺少结束事件 {start_ev.event_id}，记为未完成段"})
    return {"segments": segments, "unpaired": unpaired, "issues": issues}


# --------------------------------------------------------------------------- #
# 订单级汇总
# --------------------------------------------------------------------------- #
def _as_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def summarize_order(
    order_id: str,
    events: list[NormalizedEvent],
    paired: dict[str, Any],
    as_of: datetime,
    cyclic_stages: list[str],
    tz_issue: dict[str, str] | None,
) -> dict[str, Any]:
    """把配对结果汇总为订单轨迹：处理/等待/未完成/返工/覆盖，全部可由区间并集解释。"""
    segments = paired["segments"]
    completed = [s for s in segments if s["status"] == SEG_COMPLETED]
    open_segs = [s for s in segments if s["status"] == SEG_OPEN]
    invalid_segs = [s for s in segments if s["status"] == SEG_INVALID]

    # 返工判定：尝试编号 > 1 为真实重做；同尝试多段且区间重叠是并行作业而非返工，
    # 仅当同尝试后段在前一段结束之后才开始（顺序重做）才算返工。
    seen_stage_rounds: dict[str, int] = {}
    stage_timeline: dict[str, list[dict[str, Any]]] = {}
    for seg in sorted(segments, key=lambda s: (s["stage"], s["start_time"] or "")):
        stage_timeline.setdefault(seg["stage"], []).append(seg)
    for stage, stage_segments in stage_timeline.items():
        prior_ends: list[datetime] = []
        for round_no, seg in enumerate(stage_segments, start=1):
            seen_stage_rounds[seg["stage"]] = round_no
            start_dt = _as_dt(seg["start_time"])
            is_overlap_parallel = bool(start_dt and prior_ends and start_dt < max(prior_ends))
            if seg["attempt"] > 1:
                seg["rework"] = {"is_rework": True, "kind": "attempt-retry", "round": round_no}
            elif round_no > 1 and not is_overlap_parallel:
                seg["rework"] = {"is_rework": True, "kind": "repeat", "round": round_no}
            else:
                kind = "parallel" if is_overlap_parallel else None
                seg["rework"] = {"is_rework": False, "kind": kind, "round": round_no}
            if seg["status"] == SEG_COMPLETED:
                prior_ends.append(_as_dt(seg["end_time"]))
            elif start_dt:
                prior_ends.append(start_dt)

    completed_intervals = [
        (_as_dt(s["start_time"]), _as_dt(s["end_time"])) for s in completed
    ]
    open_intervals = [
        (_as_dt(s["start_time"]), max(as_of, _as_dt(s["start_time"]))) for s in open_segs
    ]
    for seg, interval in zip(open_segs, open_intervals):
        seg["observed_seconds"] = diff_seconds(interval[1], interval[0])

    work_union = merge_intervals(completed_intervals)
    open_union = merge_intervals(open_intervals)
    covered_union = merge_intervals(completed_intervals + open_intervals)

    processing_seconds = union_seconds(completed_intervals)
    open_observed_seconds = union_seconds(open_intervals)

    rework_intervals = [
        (_as_dt(s["start_time"]), _as_dt(s["end_time"]))
        for s in completed if s["rework"]["is_rework"]
    ]
    rework_seconds = union_seconds(rework_intervals)

    all_starts = [_as_dt(s["start_time"]) for s in segments if s["start_time"]]
    all_ends = [_as_dt(s["end_time"]) for s in completed]
    span_start = min(all_starts) if all_starts else None
    last_closed_end = max(all_ends) if all_ends else None
    # 含未完成段或无效段的订单不闭合：总历时留空，观测窗口只用于观测时长，不冒充总历时
    is_closed = bool(span_start) and not open_segs and not invalid_segs
    span_end = last_closed_end if is_closed else None
    effective_end = last_closed_end if is_closed else as_of

    waiting_gaps: list[dict[str, Any]] = []
    waiting_seconds = 0
    if span_start and covered_union:
        cursor = span_start
        for gstart, gend in covered_union:
            if gstart > cursor:
                before_ids = [s["segment_id"] for s in completed + open_segs
                              if s["start_time"] and _as_dt(s["start_time"]) == gstart]
                after_ids = [s["segment_id"] for s in completed
                             if s["end_time"] and _as_dt(s["end_time"]) == cursor]
                gap = {
                    "gap_id": f"gap-{uuid4().hex[:8]}",
                    "start": iso(cursor), "end": iso(gstart),
                    "seconds": diff_seconds(gstart, cursor),
                    "after_segment_ids": after_ids,
                    "before_segment_ids": before_ids,
                }
                waiting_gaps.append(gap)
                waiting_seconds += gap["seconds"]
            cursor = max(cursor, gend)
        if cursor < effective_end:
            gap = {
                "gap_id": f"gap-{uuid4().hex[:8]}",
                "start": iso(cursor), "end": iso(effective_end),
                "seconds": diff_seconds(effective_end, cursor),
                "after_segment_ids": [s["segment_id"] for s in completed
                                      if s["end_time"] and _as_dt(s["end_time"]) == cursor],
                "before_segment_ids": [],
            }
            waiting_gaps.append(gap)
            waiting_seconds += gap["seconds"]

    # 相互覆盖的作业段（并行/重叠识别，含同环节重叠）
    overlaps = _pairwise_overlaps(completed, open_segs, as_of)

    span_seconds = diff_seconds(span_end, span_start) if span_start and span_end else None
    covered_seconds = union_seconds(completed_intervals + open_intervals)
    explainable = (
        span_seconds is not None
        and span_seconds == covered_seconds + waiting_seconds
    )
    issues = list(paired["issues"])
    if tz_issue:
        issues.append(tz_issue)
    if cyclic_stages:
        issues.append({
            "level": ISSUE_ERROR, "code": "parent_cycle",
            "message": f"父子环节关系成环：{' -> '.join(cyclic_stages)}，层级汇总已停用，作业段配对保留",
        })
    for seg in segments:
        if seg["stage"] in cyclic_stages:
            seg["parent_chain_valid"] = False
            seg.setdefault("issues", []).append("所在环节处于父子成环链路，层级关系不可用")
        else:
            seg["parent_chain_valid"] = True

    distinct_offsets = sorted({e.offset_label for e in events if e.offset_label})
    return {
        "order_id": order_id,
        "is_closed": is_closed,
        "as_of": iso(as_of),
        "span_start": iso(span_start),
        "span_end": iso(span_end),
        "span_seconds": span_seconds,
        "observed_window_end": iso(effective_end),
        "observed_window_seconds": diff_seconds(effective_end, span_start) if span_start else None,
        "processing_seconds": processing_seconds,
        "waiting_seconds": waiting_seconds,
        "open_observed_seconds": open_observed_seconds,
        "rework_seconds": rework_seconds,
        "invalid_segment_count": len(invalid_segs),
        "segment_count": len(segments),
        "covered_seconds": covered_seconds,
        "work_union": [{"start": iso(s), "end": iso(e), "seconds": diff_seconds(e, s)}
                       for s, e in work_union],
        "open_union": [{"start": iso(s), "end": iso(e), "seconds": diff_seconds(e, s)}
                       for s, e in open_union],
        "waiting_gaps": waiting_gaps,
        "overlaps": overlaps,
        "segments": segments,
        "unpaired_candidates": paired["unpaired"],
        "issues": issues,
        "timezone_offsets": distinct_offsets,
        "parent_cycle_stages": cyclic_stages,
        "reconciliation": {
            "formula": "总历时 = 处理时间并集 + 等待时间；未完结/含无效段订单不给总历时，观测窗口另列",
            "span_seconds": span_seconds,
            "observed_window_seconds": diff_seconds(effective_end, span_start) if span_start else None,
            "covered_seconds": covered_seconds,
            "waiting_seconds": waiting_seconds,
            "open_observed_seconds": open_observed_seconds,
            "invalid_segment_count": len(invalid_segs),
            "explainable": explainable,
            "explain_note": None if explainable else "订单未闭合或含结束早于开始的无效段，总历时不可由当前区间解释，未按零耗时补算",
        },
    }


def _pairwise_overlaps(completed: list[dict[str, Any]], open_segs: list[dict[str, Any]],
                       as_of: datetime) -> list[dict[str, Any]]:
    bounded: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for seg in completed:
        bounded.append((_as_dt(seg["start_time"]), _as_dt(seg["end_time"]), seg))
    for seg in open_segs:
        start = _as_dt(seg["start_time"])
        bounded.append((start, max(as_of, start), seg))
    found: list[dict[str, Any]] = []
    for i in range(len(bounded)):
        s1, e1, seg1 = bounded[i]
        for j in range(i + 1, len(bounded)):
            s2, e2, seg2 = bounded[j]
            if s1 < e2 and s2 < e1:
                ov_start, ov_end = max(s1, s2), min(e1, e2)
                found.append({
                    "overlap_id": f"ov-{uuid4().hex[:8]}",
                    "segment_ids": [seg1["segment_id"], seg2["segment_id"]],
                    "stages": [seg1["stage"], seg2["stage"]],
                    "attempts": [seg1["attempt"], seg2["attempt"]],
                    "same_stage": seg1["stage"] == seg2["stage"],
                    "start": iso(ov_start), "end": iso(ov_end),
                    "overlap_seconds": diff_seconds(ov_end, ov_start),
                })
    return found


# --------------------------------------------------------------------------- #
# 瓶颈数据集
# --------------------------------------------------------------------------- #
def benchmark_for(stage: str, span_start: datetime | None,
                  benchmarks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """基准必须注明适用环节与周期；匹配不上时返回原因，调用方不得生成虚假得分。"""
    candidates = [b for b in benchmarks if b["stage"] == stage]
    if not candidates:
        return None, f"环节 {stage} 未配置效率基准"
    for bench in candidates:
        applies_from = _as_dt(bench.get("applies_from"))
        applies_to = _as_dt(bench.get("applies_to"))
        if span_start and applies_from and span_start < applies_from:
            continue
        if span_start and applies_to and span_start > applies_to:
            continue
        return bench, None
    return None, f"环节 {stage} 的基准周期均不覆盖本订单业务时间"


def compute_bottlenecks(order_summaries: list[dict[str, Any]],
                        benchmarks: list[dict[str, Any]]) -> dict[str, Any]:
    """反事实法：把某环节各段压缩到基准后重算订单区间并集，

    减少量才是该环节对订单时长的真实延迟贡献；并行重叠部分自动抵扣，
    因此瓶颈不会由最长单条记录决定，而由跨订单的量化贡献排序决定。
    """
    rows: list[dict[str, Any]] = []
    for summary in order_summaries:
        order_id = summary["order_id"]
        span_start = _as_dt(summary["span_start"])
        segments = summary["segments"]
        completed = [s for s in segments if s["status"] == SEG_COMPLETED]
        open_segs = [s for s in segments if s["status"] == SEG_OPEN]
        all_intervals = [(_as_dt(s["start_time"]), _as_dt(s["end_time"])) for s in completed]
        baseline_union = union_seconds(all_intervals)
        stages = sorted({s["stage"] for s in segments})

        for stage in stages:
            stage_completed = [s for s in completed if s["stage"] == stage]
            stage_open = [s for s in open_segs if s["stage"] == stage]
            stage_intervals = [(_as_dt(s["start_time"]), _as_dt(s["end_time"]))
                               for s in stage_completed]
            other_intervals = [iv for s in completed if s["stage"] != stage
                               for iv in [(_as_dt(s["start_time"]), _as_dt(s["end_time"]))]]
            work_union_stage = union_seconds(stage_intervals)
            # 边际贡献：并集整体减去移除本环节后的并集（并行共享部分不计入）
            marginal = baseline_union - union_seconds(other_intervals)
            shared = max(0, work_union_stage - marginal)

            bench, bench_reason = benchmark_for(stage, span_start, benchmarks)
            standard_total: int | None = None
            excess: int | None = None
            score: float | None = None
            delay_contribution: int | None = None
            cf_intervals: list[tuple[datetime, datetime]] = []
            if bench:
                standard = int(bench["standard_seconds"])
                standard_total = standard * len(stage_completed)
                excess = max(0, work_union_stage - standard_total)
                if work_union_stage > 0:
                    score = min(100.0, round(100.0 * standard_total / work_union_stage, 1))
                # 反事实：每段从起点压缩到基准时长，再与其他环节真实区间求并集
                for seg in stage_completed:
                    start = _as_dt(seg["start_time"])
                    clipped_end = min(_as_dt(seg["end_time"]), start + timedelta(seconds=standard))
                    cf_intervals.append((start, clipped_end))
                delay_contribution = baseline_union - union_seconds(other_intervals + cf_intervals)
            waiting_boundary = sum(
                gap["seconds"] for gap in summary["waiting_gaps"]
                if any(seg_id in {s["segment_id"] for s in completed if s["stage"] == stage}
                       for seg_id in gap["after_segment_ids"])
            )
            touches_start = any(s["start_time"] == summary["span_start"] for s in stage_completed)
            touches_end = any(s["end_time"] == summary["span_end"] for s in completed)

            contribution_intervals = [{
                "kind": "work",
                "segment_id": s["segment_id"], "attempt": s["attempt"],
                "start": s["start_time"], "end": s["end_time"],
                "seconds": s["duration_seconds"],
                "rework": s["rework"]["is_rework"],
            } for s in stage_completed]
            contribution_intervals += [{
                "kind": "open-observed",
                "segment_id": s["segment_id"], "attempt": s["attempt"],
                "start": s["start_time"], "end": summary["as_of"],
                "seconds": s["observed_seconds"],
                "rework": s["rework"]["is_rework"],
            } for s in stage_open]
            for gap in summary["waiting_gaps"]:
                if any(seg_id in {s["segment_id"] for s in completed if s["stage"] == stage}
                       for seg_id in gap["after_segment_ids"]):
                    contribution_intervals.append({
                        "kind": "waiting-gap", "segment_id": None, "attempt": None,
                        "start": gap["start"], "end": gap["end"],
                        "seconds": gap["seconds"], "rework": False,
                    })

            rows.append({
                "order_id": order_id,
                "stage": stage,
                "attempts": sorted({s["attempt"] for s in segments if s["stage"] == stage}),
                "segment_count": len(stage_completed) + len(stage_open),
                "completed_count": len(stage_completed),
                "open_count": len(stage_open),
                "invalid_count": sum(1 for s in segments
                                     if s["stage"] == stage and s["status"] == SEG_INVALID),
                "rework_seconds": union_seconds([
                    (_as_dt(s["start_time"]), _as_dt(s["end_time"]))
                    for s in stage_completed if s["rework"]["is_rework"]
                ]),
                "work_union_seconds": work_union_stage,
                "marginal_seconds": marginal,
                "shared_parallel_seconds": shared,
                "waiting_boundary_seconds": waiting_boundary,
                "benchmark": None if not bench else {
                    "benchmark_id": bench["benchmark_id"], "stage": stage,
                    "standard_seconds": standard, "period": bench["period"],
                    "applies_from": bench.get("applies_from"), "applies_to": bench.get("applies_to"),
                },
                "no_benchmark_reason": bench_reason,
                "standard_total_seconds": standard_total,
                "excess_seconds": excess,
                "efficiency_score": score,
                "delay_contribution_seconds": delay_contribution,
                "touches_span_start": touches_start,
                "touches_span_end": touches_end,
                "contribution_intervals": contribution_intervals,
            })

    # 环节级跨订单汇总：排序依据是反事实贡献总量与影响订单数，而非单条最长记录
    stage_summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        agg = stage_summary.setdefault(row["stage"], {
            "stage": row["stage"], "order_count": 0, "affected_order_count": 0,
            "work_union_seconds": 0, "marginal_seconds": 0, "shared_parallel_seconds": 0,
            "rework_seconds": 0, "waiting_boundary_seconds": 0,
            "delay_contribution_seconds": 0, "delay_contribution_available": True,
            "open_count": 0, "invalid_count": 0,
            "score_values": [], "benchmark_periods": [], "evidence_order_ids": [],
        })
        agg["order_count"] += 1
        agg["work_union_seconds"] += row["work_union_seconds"]
        agg["marginal_seconds"] += row["marginal_seconds"]
        agg["shared_parallel_seconds"] += row["shared_parallel_seconds"]
        agg["rework_seconds"] += row["rework_seconds"]
        agg["waiting_boundary_seconds"] += row["waiting_boundary_seconds"]
        agg["open_count"] += row["open_count"]
        agg["invalid_count"] += row["invalid_count"]
        if row["efficiency_score"] is not None:
            agg["score_values"].append(row["efficiency_score"])
        if row["benchmark"]:
            agg["benchmark_periods"].append(row["benchmark"]["period"])
            agg["evidence_order_ids"].append(row["order_id"])
            if (row["delay_contribution_seconds"] or 0) > 0:
                agg["affected_order_count"] += 1
                agg["delay_contribution_seconds"] += row["delay_contribution_seconds"]
        else:
            agg["delay_contribution_available"] = False
    summaries: list[dict[str, Any]] = []
    for agg in stage_summary.values():
        agg["benchmark_periods"] = sorted(set(agg["benchmark_periods"]))
        agg["average_efficiency_score"] = (
            round(sum(agg["score_values"]) / len(agg["score_values"]), 1)
            if agg["score_values"] else None
        )
        del agg["score_values"]
        summaries.append(agg)
    summaries.sort(key=lambda a: (-(a["delay_contribution_seconds"] or 0),
                                  -a["affected_order_count"], -a["marginal_seconds"]))
    for rank, agg in enumerate(summaries, start=1):
        agg["bottleneck_rank"] = rank
        agg["is_bottleneck"] = rank == 1 and (
            agg["delay_contribution_seconds"] > 0 or agg["waiting_boundary_seconds"] > 0
        )
    return {
        "generated_at": iso(datetime.now(timezone.utc)),
        "method": "反事实区间并集：压缩到基准后重算并集，并行重叠自动抵扣，不直接相加环节耗时",
        "order_stage_rows": rows,
        "stage_summary": summaries,
    }
