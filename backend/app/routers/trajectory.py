"""供应链轨迹核算接口：事件入库、版本化配对、瓶颈数据集与决策建议联动。

汇总接口默认返回可展开的贡献区间；耗时无法成立时返回 null 与原因，不补零。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import trajectory_engine as engine
from app.services.trajectory import trajectory_service
from app.trajectory_seed import (
    SEED_AS_OF,
    TRAJ_BACKFILL_EVENTS,
    TRAJ_BENCHMARKS,
    TRAJ_EVENTS,
)

router = APIRouter(prefix="/api/trajectory", tags=["供应链轨迹核算"])


class EventBatch(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "manual"
    retry: bool = False


class BenchmarkPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _seed_as_of() -> datetime:
    dt, _, err = engine.parse_business_time(SEED_AS_OF)
    return dt or datetime.now(timezone.utc)


@router.post("/seed/reset")
def reset_seed() -> dict[str, Any]:
    """重置为内置样例：基准、全部事件、O-3001 两版轨迹一次建好，便于核对。"""
    trajectory_service.reset()
    for bench in TRAJ_BENCHMARKS:
        trajectory_service.upsert_benchmark(bench)
    as_of = _seed_as_of()
    backfill_ids = {ev["event_id"] for ev in TRAJ_BACKFILL_EVENTS}
    first_batch = [ev for ev in TRAJ_EVENTS if ev["event_id"] not in backfill_ids]
    first = trajectory_service.ingest_events(first_batch, source="seed", as_of=as_of)
    second = trajectory_service.ingest_events(TRAJ_BACKFILL_EVENTS, source="backfill", as_of=as_of)
    return {
        "ok": True,
        "message": "轨迹样例已重建：含并行、返工、乱序、跨时区、未结束及四类异常样本",
        "first_ingest": first["message"],
        "backfill_ingest": second["message"],
        "order_versions": {
            oid: trajectory_service.get_order(oid)["version_count"]
            for oid in sorted(trajectory_service._versions)  # noqa: SLF001 - 种子重建回执
        },
    }


@router.post("/events")
def ingest_events(batch: EventBatch) -> dict[str, Any]:
    """事件入库并仅对受影响订单追加新轨迹版本；重复事件被拒绝，不重复占用时间。"""
    return trajectory_service.ingest_events(
        batch.events, source=batch.source or "manual", retry=batch.retry
    )


@router.post("/orders/{order_id}/retry")
def retry_pairing(order_id: str) -> dict[str, Any]:
    """配对失败后重试：用当前全部事件重新配对并生成新版本，不与历史版本混算。"""
    version = trajectory_service.recompute_order(order_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 暂无轨迹事件，无法重试配对")
    return {"ok": True, "message": f"订单 {order_id} 已重新配对并生成 {version['version_id']}",
            "version": {
                "version_id": version["version_id"],
                "pairing_failed": version["pairing_failed"],
                "fingerprint": version["fingerprint"],
                "issue_count": len(version["summary"]["issues"]),
            }}


@router.get("/orders")
def list_orders() -> dict[str, Any]:
    """订单轨迹列表：总历时、处理/等待/未完成/返工与覆盖识别，均可由区间并集解释。"""
    summaries = trajectory_service.list_orders()
    return {"total": len(summaries), "items": summaries}


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    expand: str = Query(default="segments,intervals",
                        description="可展开内容：segments/intervals/gaps/overlaps/issues/candidates，all 全展开"),
) -> dict[str, Any]:
    """订单轨迹明细，附全部历史版本；默认展开作业段与区间并集。"""
    detail = trajectory_service.get_order(order_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在轨迹数据")
    summary = detail["current"]
    if expand != "all":
        wanted = {part.strip() for part in expand.split(",") if part.strip()}
        slim = {k: v for k, v in summary.items()
                if k in {"order_id", "is_closed", "as_of", "span_start", "span_end",
                         "span_seconds", "processing_seconds", "waiting_seconds",
                         "open_observed_seconds", "rework_seconds", "invalid_segment_count",
                         "segment_count", "covered_seconds", "timezone_offsets",
                         "parent_cycle_stages", "reconciliation"}}
        if "segments" in wanted:
            slim["segments"] = summary["segments"]
        if "intervals" in wanted:
            slim["work_union"] = summary["work_union"]
            slim["open_union"] = summary["open_union"]
        if "gaps" in wanted:
            slim["waiting_gaps"] = summary["waiting_gaps"]
        if "overlaps" in wanted:
            slim["overlaps"] = summary["overlaps"]
        if "issues" in wanted:
            slim["issues"] = summary["issues"]
        if "candidates" in wanted:
            slim["unpaired_candidates"] = summary["unpaired_candidates"]
        detail["current"] = slim
    return detail


@router.get("/orders/{order_id}/segments")
def get_segments(order_id: str) -> dict[str, Any]:
    """作业段明细：关联订单、尝试编号与区间边界，附配对依据与候选。"""
    segments = trajectory_service.get_order_segments(order_id)
    if segments is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在轨迹数据")
    return {"order_id": order_id, "total": len(segments), "items": segments}


@router.get("/bottlenecks")
def bottlenecks(
    include_intervals: bool = Query(default=True, description="汇总是否附可展开的贡献区间"),
) -> dict[str, Any]:
    """瓶颈环节数据集：反事实区间并集量化延迟贡献，不直接相加环节耗时。"""
    dataset = trajectory_service.bottleneck_dataset()
    if not include_intervals:
        for row in dataset["order_stage_rows"]:
            row.pop("contribution_intervals", None)
    return dataset


@router.get("/benchmarks")
def list_benchmarks() -> dict[str, Any]:
    items = trajectory_service.list_benchmarks()
    return {"total": len(items), "items": items}


@router.post("/benchmarks")
def upsert_benchmark(payload: BenchmarkPayload) -> dict[str, Any]:
    """登记基准：必须注明适用环节与周期；缺少基准的环节不会生成效率得分。"""
    record, error = trajectory_service.upsert_benchmark(payload.values)
    if error:
        return {"ok": False, "message": error}
    return {"ok": True, "message": f"基准 {record['benchmark_id']} 已保存", "entry": record}


@router.post("/recommendations/push")
def push_recommendations() -> dict[str, Any]:
    """消费瓶颈数据集生成决策建议：量化可减少的订单时长并保存轨迹依据。"""
    return trajectory_service.push_bottleneck_recommendations()


@router.get("/recommendations")
def list_recommendations() -> dict[str, Any]:
    items = trajectory_service.list_recommendations()
    return {"total": len(items), "items": items}
