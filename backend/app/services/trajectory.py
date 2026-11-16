"""轨迹加工服务：事件入库、版本化配对、瓶颈数据集与决策建议联动。

与 ``store.py`` 的示例表分离，轨迹数据自带独立的内存仓库；
所有写入产生不可变版本，补报只追加新版本，绝不把新旧配对混为一个完整订单。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.services import trajectory_engine as engine


class TrajectoryService:
    def __init__(self) -> None:
        # 原始事件：event_id 唯一
        self._events: dict[str, dict[str, Any]] = {}
        self._benchmarks: dict[str, dict[str, Any]] = {}
        # order_id -> 版本列表（旧版本保留）
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 由瓶颈数据集推送的决策建议，附带轨迹依据
        self._recommendations: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 基准
    # ------------------------------------------------------------------ #
    def upsert_benchmark(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        stage = str(values.get("stage") or "").strip()
        period = str(values.get("period") or "").strip()
        if not stage or not period:
            return None, "基准必须注明适用环节（stage）与周期（period）"
        try:
            standard_seconds = int(values["standard_seconds"])
        except (KeyError, TypeError, ValueError):
            return None, "standard_seconds 必须是整数秒"
        if standard_seconds <= 0:
            return None, "标准耗时必须为正数，缺少基准不会补零或生成虚假得分"
        benchmark_id = str(values.get("benchmark_id") or f"BENCH-{uuid4().hex[:8]}")
        applies_from = values.get("applies_from")
        applies_to = values.get("applies_to")
        if applies_from and applies_to:
            start, _, err1 = engine.parse_business_time(applies_from)
            end, _, err2 = engine.parse_business_time(applies_to)
            if err1 or err2:
                return None, f"基准周期边界无法解析：{err1 or err2}"
            if start and end and end <= start:
                return None, "基准周期结束时间必须晚于开始时间"
        record = {
            "benchmark_id": benchmark_id,
            "stage": stage,
            "period": period,
            "standard_seconds": standard_seconds,
            "applies_from": applies_from,
            "applies_to": applies_to,
            "note": values.get("note"),
        }
        self._benchmarks[benchmark_id] = record
        return record, None

    def list_benchmarks(self) -> list[dict[str, Any]]:
        return [dict(b) for b in self._benchmarks.values()]

    # ------------------------------------------------------------------ #
    # 事件入库与版本化加工
    # ------------------------------------------------------------------ #
    def ingest_events(self, raw_events: list[dict[str, Any]], *,
                      source: str = "manual", retry: bool = False,
                      as_of: datetime | None = None) -> dict[str, Any]:
        """接收一批事件，去重后仅对受影响订单重新配对并追加轨迹版本。"""
        if not isinstance(raw_events, list):
            return {"ok": False, "message": "events 必须是数组", "versions": []}
        as_of = as_of or datetime.now(timezone.utc)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        affected_orders: set[str] = set()

        for index, raw in enumerate(raw_events):
            if not isinstance(raw, dict):
                rejected.append({"index": index, "reason": "事件必须是对象"})
                continue
            # 先做结构规范化：缺订单/环节、坏事件类型等直接拒绝，避免静默丢弃
            preview, structural_issues = engine.normalize_event(raw, index)
            hard_errors = [i for i in structural_issues if i["level"] == engine.ISSUE_ERROR]
            if preview is None:
                rejected.append({
                    "index": index,
                    "event_id": raw.get("event_id"),
                    "reason": "；".join(i["message"] for i in hard_errors),
                })
                continue
            event_id = preview.event_id
            if event_id in self._events:
                rejected.append({"event_id": event_id, "reason": "事件已存在，重复事件不重复占用时间"})
                continue
            stored = dict(raw)
            stored["event_id"] = event_id
            self._events[event_id] = stored
            accepted.append(stored)
            affected_orders.add(preview.order_id)
        versions = []
        for order_id in sorted(affected_orders):
            versions.append(self._recompute_order(order_id, source=source,
                                                  retry=retry, as_of=as_of))
        return {
            "ok": True,
            "message": f"接收 {len(accepted)} 条事件，拒绝 {len(rejected)} 条，重算 {len(versions)} 个订单",
            "accepted_count": len(accepted),
            "rejected": rejected,
            "versions": versions,
        }

    def recompute_order(self, order_id: str, *, source: str = "retry",
                        as_of: datetime | None = None) -> dict[str, Any] | None:
        """配对失败后的重试入口：用当前全部事件重新配对，仍是全新版本。"""
        if not self._order_event_ids(order_id):
            return None
        return self._recompute_order(order_id, source=source, retry=True,
                                     as_of=as_of or datetime.now(timezone.utc))

    def _order_event_ids(self, order_id: str) -> list[str]:
        return [eid for eid, ev in self._events.items()
                if str(ev.get("order_id") or "").strip() == order_id]

    def _recompute_order(self, order_id: str, *, source: str, retry: bool,
                         as_of: datetime) -> dict[str, Any]:
        raw_events = [self._events[eid] for eid in self._order_event_ids(order_id)]
        normalized: list[engine.NormalizedEvent] = []
        global_issues: list[dict[str, str]] = []
        for index, raw in enumerate(raw_events):
            event, issues = engine.normalize_event(raw, index)
            global_issues.extend(issues)
            if event is not None:
                normalized.append(event)

        normalized, dedupe_issues = engine._dedupe(normalized)
        global_issues.extend(dedupe_issues)

        # 时区不一致：同一订单出现多个时区标签时给警告，仍统一换算 UTC 后配对
        tz_issue = None
        offsets = {e.offset_label for e in normalized if e.offset_label}
        if len(offsets) > 1:
            tz_issue = {
                "level": engine.ISSUE_WARNING,
                "code": "timezone_inconsistent",
                "message": f"订单 {order_id} 存在多个时区标注 {sorted(offsets)}，已统一换算为 UTC 配对，请核对原始时间",
            }

        cyclic = engine.detect_parent_cycle(normalized)
        paired = engine.pair_order(normalized)
        summary = engine.summarize_order(order_id, normalized, paired, as_of, cyclic, tz_issue)
        summary["issues"] = global_issues + summary["issues"]

        fp = engine.fingerprint(normalized)
        history = self._versions.setdefault(order_id, [])
        previous_fp = history[-1]["fingerprint"] if history else None
        version_no = len(history) + 1
        version = {
            "version_id": f"TV-{order_id}-V{version_no}",
            "order_id": order_id,
            "version_no": version_no,
            "fingerprint": fp,
            "previous_fingerprint": previous_fp,
            "fingerprint_changed": fp != previous_fp if previous_fp else None,
            "source": source,
            "is_retry": retry,
            "created_at": engine.iso(datetime.now(timezone.utc)),
            "as_of": engine.iso(as_of),
            "event_count": len(normalized),
            "event_ids": sorted(e.event_id for e in normalized),
            "pairing_failed": bool([i for i in summary["issues"]
                                    if i["level"] == engine.ISSUE_ERROR]),
            "summary": summary,
        }
        history.append(version)
        return version

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_orders(self) -> list[dict[str, Any]]:
        return [self._versions[oid][-1]["summary"] for oid in sorted(self._versions)]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        history = self._versions.get(order_id)
        if not history:
            return None
        latest = history[-1]
        return {
            "order_id": order_id,
            "latest_version": latest["version_id"],
            "version_count": len(history),
            "current": latest["summary"],
            "versions": [{
                "version_id": v["version_id"],
                "version_no": v["version_no"],
                "fingerprint": v["fingerprint"],
                "fingerprint_changed": v["fingerprint_changed"],
                "source": v["source"],
                "is_retry": v["is_retry"],
                "created_at": v["created_at"],
                "event_count": v["event_count"],
                "pairing_failed": v["pairing_failed"],
            } for v in history],
        }

    def get_order_segments(self, order_id: str) -> list[dict[str, Any]] | None:
        history = self._versions.get(order_id)
        if not history:
            return None
        return history[-1]["summary"]["segments"]

    # ------------------------------------------------------------------ #
    # 瓶颈数据集
    # ------------------------------------------------------------------ #
    def bottleneck_dataset(self) -> dict[str, Any]:
        summaries = self.list_orders()
        dataset = engine.compute_bottlenecks(summaries, self.list_benchmarks())
        dataset["order_count"] = len(summaries)
        dataset["benchmark_count"] = len(self._benchmarks)
        return dataset

    # ------------------------------------------------------------------ #
    # 决策建议推送：消费瓶颈数据集，量化可减少的订单时长并保存轨迹依据
    # ------------------------------------------------------------------ #
    def push_bottleneck_recommendations(self) -> dict[str, Any]:
        dataset = self.bottleneck_dataset()
        rows = dataset["order_stage_rows"]
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        existing_stages = {rec["stage"] for rec in self._recommendations}
        for agg in dataset["stage_summary"]:
            if agg["stage"] in existing_stages:
                # 幂等：同环节已推送过则复用既有建议，避免重复推送、重复登记
                skipped.append(agg["stage"])
                continue
            stage = agg["stage"]
            stage_rows = [r for r in rows if r["stage"] == stage]
            evidence = [{
                "order_id": r["order_id"],
                "work_union_seconds": r["work_union_seconds"],
                "marginal_seconds": r["marginal_seconds"],
                "shared_parallel_seconds": r["shared_parallel_seconds"],
                "delay_contribution_seconds": r["delay_contribution_seconds"],
                "waiting_boundary_seconds": r["waiting_boundary_seconds"],
                "rework_seconds": r["rework_seconds"],
                "benchmark": r["benchmark"],
                "no_benchmark_reason": r["no_benchmark_reason"],
                "contribution_intervals": r["contribution_intervals"],
            } for r in stage_rows]
            if agg["delay_contribution_available"] and agg["delay_contribution_seconds"] > 0:
                option = (
                    f"压缩{stage}至基准：按反事实区间并集测算，可减少订单时长合计 "
                    f"{agg['delay_contribution_seconds']} 秒，覆盖 {agg['affected_order_count']} 个订单"
                    f"（并行重叠 {agg['shared_parallel_seconds']} 秒已抵扣，未直接相加环节耗时）"
                )
                rationale = "quantified-delay-contribution"
            elif not agg["delay_contribution_available"]:
                option = (
                    f"补录{stage}效率基准后再评估：当前缺少适用基准，"
                    "只提供实际作业并集与边际贡献，不生成虚假效率得分"
                )
                rationale = "missing-benchmark"
            else:
                option = (
                    f"维持{stage}：各订单实际作业未超基准，延迟贡献为 0；"
                    f"关注其等待边界 {agg['waiting_boundary_seconds']} 秒与返工 {agg['rework_seconds']} 秒"
                )
                rationale = "no-excess"
            record = {
                "recommendation_id": f"TRAJ-DEC-{uuid4().hex[:8]}",
                "stage": stage,
                "bottleneck_rank": agg["bottleneck_rank"],
                "is_bottleneck": agg["is_bottleneck"],
                "analysis_subject": f"供应链轨迹瓶颈改善：{stage}",
                "decision_option": option,
                "rationale": rationale,
                "quantified": {
                    "delay_contribution_seconds": agg["delay_contribution_seconds"],
                    "affected_order_count": agg["affected_order_count"],
                    "marginal_seconds": agg["marginal_seconds"],
                    "shared_parallel_seconds": agg["shared_parallel_seconds"],
                    "rework_seconds": agg["rework_seconds"],
                    "waiting_boundary_seconds": agg["waiting_boundary_seconds"],
                    "average_efficiency_score": agg["average_efficiency_score"],
                    "benchmark_periods": agg["benchmark_periods"],
                },
                "trajectory_evidence": evidence,
                "created_at": engine.iso(datetime.now(timezone.utc)),
                "status": "待审核",
            }
            self._recommendations.append(record)
            created.append(record)
        return {
            "ok": True,
            "message": (f"已基于瓶颈数据集生成 {len(created)} 条决策建议，全部附带可展开的轨迹贡献区间"
                        + (f"；{len(skipped)} 个环节已推送过，本次跳过：{'、'.join(skipped)}" if skipped else "")),
            "created": created,
            "skipped_stages": skipped,
            "dataset_method": dataset["method"],
        }

    def list_recommendations(self) -> list[dict[str, Any]]:
        return list(self._recommendations)

    def reset(self) -> None:
        self._events.clear()
        self._benchmarks.clear()
        self._versions.clear()
        self._recommendations.clear()


trajectory_service = TrajectoryService()
