"""供应链效率监控业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.trajectory import trajectory_service
from app.store import store

MODULE = "supply"
REQUIRED_FIELDS = ["指标编号", "环节名称", "当前耗时"]
STATUS_ORDER = ["正常", "效率低", "瓶颈中", "已优化"]
ACTION_RULES = {"标记瓶颈": "正常", "执行优化": "效率低", "标记优化": "瓶颈中"}
NEGATIVE_ACTIONS = []


class SupplyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"效率指标 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于供应链效率监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"效率指标已{action}"

    def trajectory_metrics(self) -> dict[str, Any]:
        """基于轨迹核算的效率口径：替代示例耗时，数字都能回溯到订单区间并集。"""
        orders = trajectory_service.list_orders()
        dataset = trajectory_service.bottleneck_dataset()
        closed = [o for o in orders if o["is_closed"]]
        return {
            "source": "trajectory-engine",
            "order_total": len(orders),
            "closed_order_total": len(closed),
            "open_order_total": sum(1 for o in orders if not o["is_closed"]),
            "aggregated": {
                "span_seconds": sum(o["span_seconds"] or 0 for o in closed),
                "processing_union_seconds": sum(o["processing_seconds"] for o in orders),
                "waiting_seconds": sum(o["waiting_seconds"] for o in orders),
                "open_observed_seconds": sum(o["open_observed_seconds"] for o in orders),
                "rework_seconds": sum(o["rework_seconds"] for o in orders),
                "invalid_segment_total": sum(o["invalid_segment_count"] for o in orders),
            },
            "bottleneck_ranking": [
                {
                    "rank": item["bottleneck_rank"],
                    "stage": item["stage"],
                    "is_bottleneck": item["is_bottleneck"],
                    "delay_contribution_seconds": item["delay_contribution_seconds"],
                    "marginal_seconds": item["marginal_seconds"],
                    "shared_parallel_seconds": item["shared_parallel_seconds"],
                    "average_efficiency_score": item["average_efficiency_score"],
                    "benchmark_periods": item["benchmark_periods"],
                }
                for item in dataset["stage_summary"]
            ],
            "method": dataset["method"],
            "note": "当前耗时/标准耗时示例字段保留在原表；本口径来自事件配对与区间并集，无基准环节不生成得分",
        }
