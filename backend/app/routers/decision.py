"""决策建议推送接口：维护决策建议，覆盖推送建议、标记采纳、驳回建议等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.decision import DecisionService

router = APIRouter(prefix="/api/decision", tags=["决策建议推送"])

service = DecisionService()

LIST_FIELDS = ["建议编号", "分析主题", "决策选项", "收益评估", "风险评估", "推荐方案", "生成时间", "建议状态"]
STATUSES = ["待审核", "已推送", "已采纳", "已驳回"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按建议编号检索"),
    status: str | None = Query(default=None, description="待审核、已推送、已采纳、已驳回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按建议编号与状态过滤决策建议推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定字面量路径必须声明在 /{entry_id} 通配路由之前，否则会被当成条目 id 解析。
@router.get("/trajectory-recommendations")
def trajectory_recommendations() -> dict[str, Any]:
    """读取轨迹驱动的决策建议，附每个环节在各订单上的可展开贡献区间。"""
    return service.list_trajectory_recommendations()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出决策建议推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "decision", "total": total, "items": items}


@router.post("/trajectory-consume")
def consume_trajectory_bottlenecks() -> dict[str, Any]:
    """跨模块消费瓶颈环节数据集：按量化延迟贡献推送建议，轨迹依据随建议保存。"""
    return service.consume_bottlenecks()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条决策建议明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"决策建议 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条决策建议，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="决策建议已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条决策建议执行推送建议、标记采纳、驳回建议；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
