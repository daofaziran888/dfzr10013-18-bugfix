"""决策建议推送业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.trajectory import trajectory_service
from app.store import store

MODULE = "decision"
REQUIRED_FIELDS = ["建议编号", "分析主题", "决策选项"]
STATUS_ORDER = ["待审核", "已推送", "已采纳", "已驳回"]
ACTION_RULES = {"推送建议": "待审核", "标记采纳": "已推送", "驳回建议": "已采纳"}
NEGATIVE_ACTIONS = ["驳回建议"]


class DecisionService:
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
            rows = [row for row in rows if keyword in str(row.get("建议编号", ""))]
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
            return None, f"决策建议 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于决策建议推送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"决策建议已{action}"

    def consume_bottlenecks(self) -> dict[str, Any]:
        """跨模块消费供应链瓶颈数据集：按实际延迟贡献生成建议并保存轨迹依据。"""
        pushed = trajectory_service.push_bottleneck_recommendations()
        # 推送接口幂等时 created 可能为空，这里以全部轨迹建议为准同步登记，
        # 保证决策模块始终能查到带轨迹依据的建议且不重复登记。
        all_records = trajectory_service.list_recommendations()
        existing = {row.get("建议编号") for row in store.rows(MODULE)}
        synced = 0
        for record in all_records:
            if record["recommendation_id"] in existing:
                continue
            rows = store.rows(MODULE)
            row = {"id": max((int(item.get("id", 0)) for item in rows), default=0) + 1}
            row.update({
                "status": "待审核",
                "pending": True,
                "abnormal": False,
                "建议编号": record["recommendation_id"],
                "分析主题": record["analysis_subject"],
                "决策选项": record["decision_option"],
                "收益评估": (
                    f"预计减少订单时长 {record['quantified']['delay_contribution_seconds']} 秒，"
                    f"影响 {record['quantified']['affected_order_count']} 个订单"
                ),
                "风险评估": "基于当前事件版本与基准周期，补报事件会产生新轨迹版本，需重新评估",
                "推荐方案": record["rationale"],
                "生成时间": record["created_at"][:10],
                "建议状态": "待审核",
                "轨迹依据": {
                    "bottleneck_rank": record["bottleneck_rank"],
                    "quantified": record["quantified"],
                    "evidence_orders": [
                        item["order_id"] for item in record["trajectory_evidence"]
                    ],
                },
            })
            rows.append(row)
            synced += 1
        pushed["synced_to_decision"] = synced
        return pushed

    def list_trajectory_recommendations(self) -> dict[str, Any]:
        """读取由瓶颈数据集推送的建议（含可展开的贡献区间依据）。"""
        items = trajectory_service.list_recommendations()
        return {"total": len(items), "items": items,
                "method": "反事实区间并集量化延迟贡献，并行重叠抵扣，不直接相加环节耗时"}
