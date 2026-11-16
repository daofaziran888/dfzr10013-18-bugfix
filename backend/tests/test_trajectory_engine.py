"""轨迹核算端到端核对：覆盖题目五类样本与四类异常、版本化、瓶颈与决策联动。

运行（在仓库根或 backend 下均可）：
  python3 -I /workspace/backend/tests/test_trajectory_engine.py
不依赖第三方库；失败时以非零退出码返回并打印差异。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.path.insert(0, "/workspace/backend")

from app.services import trajectory_engine as engine  # noqa: E402
from app.services.trajectory import TrajectoryService  # noqa: E402
from app.trajectory_seed import (  # noqa: E402
    SEED_AS_OF,
    TRAJ_BACKFILL_EVENTS,
    TRAJ_BENCHMARKS,
    TRAJ_EVENTS,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name} :: {detail}")


def as_of() -> datetime:
    dt, _, err = engine.parse_business_time(SEED_AS_OF)
    assert dt and not err
    return dt


def build_service() -> TrajectoryService:
    # 复用模块级单例：决策服务与路由都消费同一个轨迹仓库，重置后重建样例
    from app.services.trajectory import trajectory_service as svc
    svc.reset()
    for bench in TRAJ_BENCHMARKS:
        record, err = svc.upsert_benchmark(bench)
        assert record and not err
    backfill_ids = {ev["event_id"] for ev in TRAJ_BACKFILL_EVENTS}
    first_batch = [ev for ev in TRAJ_EVENTS if ev["event_id"] not in backfill_ids]
    svc.ingest_events(first_batch, source="seed", as_of=as_of())
    svc.ingest_events(TRAJ_BACKFILL_EVENTS, source="backfill", as_of=as_of())
    return svc


def main() -> int:
    svc = build_service()
    orders = {o["order_id"]: o for o in svc.list_orders()}

    # 1. 两环节并行：并集而非相加 -------------------------------------------
    o1001 = orders["O-1001"]
    check("并行订单闭合", o1001["is_closed"])
    check("并行处理时间取并集(720+300=1020，非600+420+300=1320)",
          o1001["processing_seconds"] == 1020, o1001["processing_seconds"])
    check("并行总历时可解释", o1001["reconciliation"]["explainable"])
    check("识别跨环节覆盖段", len(o1001["overlaps"]) == 1
          and o1001["overlaps"][0]["overlap_seconds"] == 300)
    check("等待时间480s(8分钟打包->发货空窗)", o1001["waiting_seconds"] == 480,
          o1001["waiting_seconds"])

    # 2. 返工两轮 -----------------------------------------------------------
    o1002 = orders["O-1002"]
    check("返工订单闭合", o1002["is_closed"])
    check("返工并集1920s(900+600+420，首轮480被后续覆盖)",
          o1002["rework_seconds"] == 1920, o1002["rework_seconds"])
    rework_kinds = {s["rework"]["kind"] for s in o1002["segments"] if s["rework"]["is_rework"]}
    check("区分attempt-retry返工", rework_kinds == {"attempt-retry"}, rework_kinds)
    check("返工订单总历时可解释", o1002["reconciliation"]["explainable"])

    # 3. 乱序结束：配对依据保留候选 ----------------------------------------
    o1003 = orders["O-1003"]
    ambiguous = [s for s in o1003["segments"] if s["pairing"]["confidence"] == "ambiguous"]
    check("乱序上报仍按业务时间闭合", o1003["is_closed"] and len(ambiguous) >= 1)
    check("不确定配对保留两个开始候选",
          ambiguous and set(ambiguous[0]["pairing"]["candidate_start_event_ids"]) == {"E-1003-01", "E-1003-02"})
    check("同尝试重叠识别为并行而非返工",
          all(not s["rework"]["is_rework"] for s in o1003["segments"]))
    check("乱序订单覆盖段被识别", len(o1003["overlaps"]) == 1)

    # 4. 跨时区 -------------------------------------------------------------
    o1004 = orders["O-1004"]
    check("跨时区统一UTC后闭合", o1004["is_closed"])
    check("跨时区给出时区不一致原因",
          any(i["code"] == "timezone_inconsistent" for i in o1004["issues"]))
    check("跨时区时长换算正确(300+480=780)", o1004["processing_seconds"] == 780,
          o1004["processing_seconds"])
    check("保留多个时区标签", len(o1004["timezone_offsets"]) == 3, o1004["timezone_offsets"])

    # 5. 长时间未结束 ------------------------------------------------------
    o1005 = orders["O-1005"]
    check("未结束订单不闭合", not o1005["is_closed"])
    check("未结束订单总历时留空不补零", o1005["span_seconds"] is None)
    open_seg = next(s for s in o1005["segments"] if s["status"] == engine.SEG_OPEN)
    check("未完成段duration为空但有observed_seconds",
          open_seg["duration_seconds"] is None and open_seg["observed_seconds"] > 0)
    check("未完成段进入未配对候选并说明原因",
          any(c["event_id"] == "E-1005-03" for c in o1005["unpaired_candidates"]))
    check("未完结订单不强行可解释", o1005["reconciliation"]["explainable"] is False
          and o1005["reconciliation"]["explain_note"])

    # 6. 结束早于开始 ------------------------------------------------------
    o2001 = orders["O-2001"]
    invalid = [s for s in o2001["segments"] if s["status"] == engine.SEG_INVALID]
    check("结束早于开始标记invalid", len(invalid) == 1 and invalid[0]["raw_delta_seconds"] == -300)
    check("无效段耗时为None不补零", invalid[0]["duration_seconds"] is None)
    check("无效段附原因", any(i["code"] == "end_before_start" for i in o2001["issues"]))
    check("多余结束事件给缺开始原因", any(i["code"] == "missing_start" for i in o2001["issues"]))
    check("含无效段不闭合且无虚假总历时", not o2001["is_closed"] and o2001["span_seconds"] is None)

    # 7. 缺一侧事件 --------------------------------------------------------
    o2002 = orders["O-2002"]
    check("缺结束事件记未完成段", any(i["code"] == "missing_end" for i in o2002["issues"]))
    check("缺开始事件给未配对候选", any(i["code"] == "missing_start" for i in o2002["issues"]))
    check("缺侧订单不闭合", not o2002["is_closed"])

    # 8. 父子成环 ----------------------------------------------------------
    o2003 = orders["O-2003"]
    check("父子成环被识别", set(o2003["parent_cycle_stages"]) == {"打包", "发货", "质检"},
          o2003["parent_cycle_stages"])
    check("成环订单仍完成作业段配对", o2003["is_closed"]
          and all(s["parent_chain_valid"] is False for s in o2003["segments"]))
    check("成环给出error级原因", any(i["code"] == "parent_cycle" and i["level"] == "error"
                                     for i in o2003["issues"]))

    # 9. 重复事件不重复占用时间 --------------------------------------------
    o2004 = orders["O-2004"]
    check("重复上报去重后仅一个作业段", len(o2004["segments"]) == 1
          and o2004["segments"][0]["duration_seconds"] == 480)
    check("去重保留审计告警", any("duplicate" in i["code"] for i in o2004["issues"]))

    # 10. 补报产生新版本，不混算 -------------------------------------------
    detail = svc.get_order("O-3001")
    check("补报形成两个轨迹版本", detail["version_count"] == 2)
    v1, v2 = detail["versions"]
    check("新版本指纹变化", v2["fingerprint_changed"] is True
          and v2["fingerprint"] != v1["fingerprint"])
    check("V1只有拣货事件", v1["event_count"] == 2 and v2["event_count"] == 4)
    check("旧版本仍保留可审计", v1["version_id"] != v2["version_id"])

    # 11. 配对失败可重试 ----------------------------------------------------
    retry_version = svc.recompute_order("O-2001")
    check("重试生成新版本而非覆盖", retry_version["version_no"] == 2
          and retry_version["is_retry"] is True)
    check("重试不把新旧配对混成完整订单",
          svc.get_order("O-2001")["current"]["is_closed"] is False)

    # 12. 瓶颈：反事实贡献，非最长记录决定 ---------------------------------
    dataset = svc.bottleneck_dataset()
    ranking = {row["stage"]: row for row in dataset["stage_summary"]}
    check("瓶颈首位是拣货(贡献660)", ranking["拣货"]["is_bottleneck"]
          and ranking["拣货"]["delay_contribution_seconds"] == 660)
    check("并行重叠被抵扣(拣货shared=300)", ranking["拣货"]["shared_parallel_seconds"] == 300)
    # 反事实手算复核：O-1002 拣货三段并集 1980s，压到基准 3*600=1800 后整体并集减 300s
    # （中间返工段同时左右越界，压缩后仍覆盖原并集缺口，故贡献不是简单的 1980-1800=180）
    o1002_row = next(r for r in dataset["order_stage_rows"]
                     if r["order_id"] == "O-1002" and r["stage"] == "拣货")
    check("O-1002拣货反事实贡献按并集重算为300s", o1002_row["delay_contribution_seconds"] == 300,
          o1002_row["delay_contribution_seconds"])
    check("每条有作业的瓶颈行附可展开贡献区间",
          all(len(r["contribution_intervals"]) > 0
              for r in dataset["order_stage_rows"]
              if r["completed_count"] + r["open_count"] > 0))
    check("纯无效段行不伪造区间",
          all(len(r["contribution_intervals"]) == 0
              for r in dataset["order_stage_rows"]
              if r["completed_count"] + r["open_count"] == 0))

    # 13. 无基准不生成虚假得分（质检环节故意未配置基准） -------------------
    no_bench = [r for r in dataset["order_stage_rows"] if r["no_benchmark_reason"]]
    check("存在无基准环节样本", any(r["stage"] == "质检" for r in no_bench))
    check("无基准行得分留空并说明原因",
          all(r["efficiency_score"] is None and r["no_benchmark_reason"] for r in no_bench)
          and bool(no_bench))
    qc_summary = next(s for s in dataset["stage_summary"] if s["stage"] == "质检")
    check("无基准环节不生成延迟贡献与得分",
          qc_summary["average_efficiency_score"] is None
          and qc_summary["delay_contribution_available"] is False)
    bad = TrajectoryService()
    record, err = bad.upsert_benchmark({"stage": "x", "period": "p", "standard_seconds": 0})
    check("非正基准被拒绝", record is None and "正数" in err)
    record2, err2 = bad.upsert_benchmark({"stage": "x"})
    check("缺环节/周期基准被拒绝", record2 is None and "周期" in err2)

    # 14. 决策建议联动并保存轨迹依据 ---------------------------------------
    pushed = svc.push_bottleneck_recommendations()
    check("每环节生成一条建议", len(pushed["created"]) == len(dataset["stage_summary"]))
    top = next(c for c in pushed["created"] if c["is_bottleneck"])
    check("建议含量化可减少时长", top["quantified"]["delay_contribution_seconds"] == 660)
    check("建议保存订单级轨迹依据",
          all("contribution_intervals" in ev for ev in top["trajectory_evidence"]))
    recos = svc.list_recommendations()
    check("建议可回查且附区间", len(recos) == len(dataset["stage_summary"])
          and all(len(ev["trajectory_evidence"]) > 0 for ev in recos))

    # 15. 决策服务跨模块消费并同步登记（复用同一轨迹仓库，验证幂等） -------
    from app.services.decision import DecisionService
    decision_svc = DecisionService()
    result = decision_svc.consume_bottlenecks()
    check("决策模块跨模块消费瓶颈数据集(二次消费幂等)",
          result["ok"] and result["skipped_stages"])
    check("决策模块可读取轨迹建议及贡献区间",
          decision_svc.list_trajectory_recommendations()["total"] == len(dataset["stage_summary"]))
    store_rows = decision_svc.list_entries(page=1, size=100)[0]
    linked = [r for r in store_rows if str(r.get("建议编号", "")).startswith("TRAJ-DEC-")]
    check("轨迹建议登记到决策模块并带轨迹依据",
          bool(linked) and all("轨迹依据" in r for r in linked))
    second = decision_svc.consume_bottlenecks()
    check("重复消费不重复登记", len(second["created"]) == 0
          and len(decision_svc.list_entries(page=1, size=100)[0]) == len(store_rows))

    # 16. 重复事件入库被拒绝 ------------------------------------------------
    dup = svc.ingest_events([TRAJ_EVENTS[0]])
    check("重复event_id入库被拒绝", dup["accepted_count"] == 0
          and "重复" in dup["rejected"][0]["reason"])

    # 16b. 结构性坏事件入库被拒绝而非静默丢弃 -------------------------------
    bad_batch = svc.ingest_events([
        {"event_id": "BAD-1", "stage": "拣货", "event_type": "start",
         "business_time": "2026-11-15T08:00:00Z"},  # 缺 order_id
        {"event_id": "BAD-2", "order_id": "O-8888", "stage": "拣货",
         "event_type": "pause", "business_time": "2026-11-15T08:00:00Z"},  # 坏类型
        "not-an-object",
    ])
    check("结构坏事件全部拒绝且给原因", bad_batch["accepted_count"] == 0
          and len(bad_batch["rejected"]) == 3
          and all(r.get("reason") for r in bad_batch["rejected"]),
          bad_batch["rejected"])
    check("被拒绝事件不产生空订单", svc.get_order("O-8888") is None)

    # 16c. 基准周期不覆盖业务时间时不生成得分 -------------------------------
    period_svc = TrajectoryService()
    period_svc.upsert_benchmark({
        "benchmark_id": "B-OLD", "stage": "拣货", "period": "2025Q4",
        "standard_seconds": 100,
        "applies_from": "2025-10-01T00:00:00Z", "applies_to": "2025-12-31T23:59:59Z",
    })
    period_svc.ingest_events([{
        "event_id": "P-1", "order_id": "O-PERIOD", "stage": "拣货", "attempt": 1,
        "event_type": "start", "business_time": "2026-11-15T08:00:00Z",
    }, {
        "event_id": "P-2", "order_id": "O-PERIOD", "stage": "拣货", "attempt": 1,
        "event_type": "end", "business_time": "2026-11-15T08:05:00Z",
    }], as_of=as_of())
    p_ds = period_svc.bottleneck_dataset()
    p_row = next(r for r in p_ds["order_stage_rows"] if r["order_id"] == "O-PERIOD")
    check("基准周期不覆盖时给原因且无虚假得分",
          p_row["efficiency_score"] is None and "周期" in p_row["no_benchmark_reason"],
          p_row["no_benchmark_reason"])

    # 17. 总历时与区间并集对账 ---------------------------------------------
    for oid, summary in orders.items():
        if summary["is_closed"]:
            check(f"{oid} 总历时=并集+等待",
                  summary["span_seconds"] == summary["covered_seconds"] + summary["waiting_seconds"],
                  (summary["span_seconds"], summary["covered_seconds"], summary["waiting_seconds"]))

    # 18. 时间解析边界 ------------------------------------------------------
    dt, label, err = engine.parse_business_time("2026-11-10T16:00:00+08:00")
    check("带时区时间转UTC", dt == datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc) and not err)
    dt2, label2, err2 = engine.parse_business_time("2026-11-10T08:00:00")
    check("naive时间标注按UTC", label2 == "naive(按UTC处理)" and dt2.tzinfo is not None)
    dt3, _, err3 = engine.parse_business_time("not-a-time")
    check("坏时间返回原因而非抛异常", dt3 is None and bool(err3))

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项核对失败：")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部核对通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
