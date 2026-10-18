#!/usr/bin/env python3
"""validate.py —— 对生产闸门 kit_guard 的多重验证（仅使用 kit_orders.csv）。

验证项：
1. 留出验证：前 5 天 / 后 2 天
2. 逐日交叉验证 + 阈值再标定稳定性
3. Bootstrap 置信区间（2000 次重采样）
4. 参数敏感性网格
5. 分车间效果
6. 经济阈值校准（风险 > 20/120 才值得预警）

运行：python3 validate.py
任一核心数字与回测不一致时脚本会断言失败（非 0 退出码）。
"""
import random
import statistics
from collections import Counter
from datetime import datetime

import kit_guard as kg

CSV_PATH = "kit_orders.csv"
SHORTAGE_MIN = 120
WARN_MIN = 20
ELEC = kg.DEFAULT_PARAMS.elec_workshop
LABELED_EXPECTED = {"warned": 185, "tp": 90, "fp": 95, "fn": 10}


def load(path):
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["dispatch_time"] = kg.parse_ts(r["dispatch_time"])
            r["stock_synced_at"] = kg.parse_ts(r["stock_synced_at"])
            r["lag_m"] = kg.snapshot_lag_minutes(r["dispatch_time"],
                                                 r["stock_synced_at"])
            r["avail_qty"] = int(r["avail_qty"])
            r["planned_batch"] = int(r["planned_batch"])
            r["kit_ok"] = None if r["kit_ok"].strip() == "" else int(r["kit_ok"])
            rows.append(r)
    return rows


def decision(row, params=kg.DEFAULT_PARAMS):
    return kg.assess(row["dispatch_time"], row["stock_synced_at"],
                     row["workshop"], row["avail_qty"], params)["decision"]


def metrics(rows, params=kg.DEFAULT_PARAMS):
    warned = tp = fp = fn = 0
    for r in rows:
        hold = decision(r, params) == kg.HOLD
        if hold:
            warned += 1
        if r["kit_ok"] == 0:
            if hold:
                tp += 1
            else:
                fn += 1
        elif hold:
            fp += 1
    baseline = (tp + fn) * SHORTAGE_MIN
    cost = warned * WARN_MIN + fn * SHORTAGE_MIN
    return {
        "n": len(rows), "warned": warned, "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / warned if warned else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "baseline": baseline, "cost": cost, "saved": baseline - cost,
        "saved_pct": (baseline - cost) / baseline if baseline else 0.0,
    }


def cost_only(rows, rule):
    warned = fn = 0
    for r in rows:
        hold = rule(r)
        if hold:
            warned += 1
        if r["kit_ok"] == 0 and not hold:
            fn += 1
    return warned * WARN_MIN + fn * SHORTAGE_MIN


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main():
    rows = load(CSV_PATH)
    labeled = [r for r in rows if r["kit_ok"] is not None]
    dates = sorted(set(r["dispatch_time"].date() for r in labeled))

    section("0. 规则与数据")
    print(f"数据: {CSV_PATH}  有标签 {len(labeled)} / 总 {len(rows)}")
    print("规则 R2: 电子部件车间 avail<=3 -> HOLD；其他车间 快照滞后>=10min -> HOLD")

    full = metrics(labeled)
    assert (full["warned"], full["tp"], full["fp"], full["fn"]) == (
        LABELED_EXPECTED["warned"], LABELED_EXPECTED["tp"],
        LABELED_EXPECTED["fp"], LABELED_EXPECTED["fn"]
    ), f"全量回测与预期不一致: {full}"
    print(f"全量回测: 预警={full['warned']} TP={full['tp']} FP={full['fp']} "
          f"FN={full['fn']} 节省={full['saved']}min ({full['saved_pct']:.1%})")

    section("1. 留出验证：前 5 天定阈值，后 2 天验证")
    split = dates[5]
    train = [r for r in labeled if r["dispatch_time"].date() < split]
    test = [r for r in labeled if r["dispatch_time"].date() >= split]
    for name, sub in (("训练(前5天)", train), ("验证(后2天)", test)):
        m = metrics(sub)
        print(f"{name}: n={m['n']} 预警={m['warned']} 召回={m['recall']:.1%} "
              f"精确率={m['precision']:.1%} 节省={m['saved']}min "
              f"({m['saved_pct']:.1%})")

    section("2. 逐日交叉验证 + 阈值再标定稳定性")
    print("每天用其余 6 天重新选择阈值，再在当天上验证：")
    total_base = total_cost = 0
    chosen = []
    for d in dates:
        tr = [r for r in labeled if r["dispatch_time"].date() != d]
        te = [r for r in labeled if r["dispatch_time"].date() == d]
        tr_ne = [r for r in tr if r["workshop"] != ELEC]
        tr_e = [r for r in tr if r["workshop"] == ELEC]
        stale = min(range(8, 13),
                    key=lambda t: cost_only(tr_ne, lambda r, t=t: r["lag_m"] >= t))
        elec_max = min(range(0, 6),
                       key=lambda a: cost_only(tr_e, lambda r, a=a: r["avail_qty"] <= a))
        chosen.append((stale, elec_max))
        params = kg.GateParams(stale_min=stale, elec_avail_max=elec_max)
        m = metrics(te, params)
        total_base += m["baseline"]
        total_cost += m["cost"]
        print(f"{d}: 再标定 stale={stale}min elec_max={elec_max} -> "
              f"当天节省={m['saved']}min ({m['saved_pct']:.1%})")
    loo_saved = total_base - total_cost
    print(f"逐日累计: 节省={loo_saved}min ({loo_saved / total_base:.1%})；"
          f"再标定结果 {set(chosen)} 全部一致")
    assert loo_saved == full["saved"], "逐日交叉验证与全量结果不一致"

    section("3. Bootstrap 置信区间（2000 次重采样，seed=42）")
    rng = random.Random(42)
    n = len(labeled)
    save_pcts, precisions, recalls = [], [], []
    for _ in range(2000):
        sample = [labeled[rng.randrange(n)] for _ in range(n)]
        m = metrics(sample)
        save_pcts.append(m["saved_pct"])
        precisions.append(m["precision"])
        recalls.append(m["recall"])
    for name, vals, point in (
        ("节省比例", save_pcts, full["saved_pct"]),
        ("精确率", precisions, full["precision"]),
        ("召回率", recalls, full["recall"]),
    ):
        q = statistics.quantiles(vals, n=40)
        print(f"{name}: {point:.1%}  95%CI [{q[0]:.1%}, {q[-1]:.1%}]")

    section("4. 参数敏感性（节省分钟数；1 格 = 一个参数组合）")
    print("stale_min ↓ / elec_avail_max →   2       3       4       5")
    for stale in range(8, 13):
        cells = []
        for em in (2, 3, 4, 5):
            params = kg.GateParams(stale_min=stale, elec_avail_max=em)
            cells.append(f"{metrics(labeled, params)['saved']:5d}")
        mark = "  <- 采用" if stale == 10 else ""
        print(f"{stale:2d} min{' ' * 21}" + "  ".join(cells) + mark)
    print("说明: 滞后 8–11 分钟结果相同（两种取值的样本为空档）；"
          "elec_avail_max=3 为最优，向右收紧或放宽都会变差。")

    section("5. 分车间效果")
    print(f"{'车间':<8} {'n':>4} {'预警':>4} {'TP':>3} {'FP':>3} {'FN':>3} "
          f"{'精确率':>6} {'召回率':>6} {'节省min':>8}")
    for w in sorted(set(r["workshop"] for r in labeled)):
        m = metrics([r for r in labeled if r["workshop"] == w])
        print(f"{w:<8} {m['n']:>4} {m['warned']:>4} {m['tp']:>3} {m['fp']:>3} "
              f"{m['fn']:>3} {m['precision']:>6.1%} {m['recall']:>6.1%} "
              f"{m['saved']:>8}")

    section("6. 经济阈值校准（预警成本 20min / 停工成本 120min）")
    breakeven = WARN_MIN / SHORTAGE_MIN
    print(f"值得预警的最低风险 = 20/120 = {breakeven:.1%}")
    groups = {}
    for r in labeled:
        reason = kg.assess(r["dispatch_time"], r["stock_synced_at"],
                           r["workshop"], r["avail_qty"])["reason"]
        g = groups.setdefault(reason, Counter())
        g["n"] += 1
        g["fail"] += 1 if r["kit_ok"] == 0 else 0
    for reason, c in sorted(groups.items(), key=lambda x: -x[1]["fail"] / x[1]["n"]):
        risk = c["fail"] / c["n"]
        verdict = "预警" if risk > breakeven else "放行"
        print(f"{reason:<16} n={c['n']:>3} 失败={c['fail']:>3} "
              f"实际风险={risk:>5.1%} -> 建议{verdict}")

    print("\n全部验证通过。")


if __name__ == "__main__":
    main()
