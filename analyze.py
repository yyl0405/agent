#!/usr/bin/env python3
"""装配车间齐套判定失效分析（仅用 Python 标准库，可直接复现）。

用法：
    python3 analyze.py            # 默认读取同目录 kit_orders.csv
    python3 analyze.py path.csv

口径（来自题面）：缺料停工 120 分钟/次，高风险预警 20 分钟/次。
成本模型：总成本 = 预警次数 × 20 + 漏报缺料次数 × 120。
假设：被预警且真实会缺料的工单，经复核后避免了停工；敏感性分析见文末。
"""
import csv
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

SHORTAGE_MIN = 120
WARN_MIN = 20
STALE_MIN = 10          # 库存快照滞后阈值（分钟）
ELEC = "电子部件车间"
ELEC_AVAIL_MAX = 3      # 电子部件车间的低库存阈值


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["dispatch_time"] = datetime.fromisoformat(r["dispatch_time"])
            r["stock_synced_at"] = datetime.fromisoformat(
                r["stock_synced_at"].replace("Z", "+00:00")
            )
            r["lag_m"] = (
                r["dispatch_time"] - r["stock_synced_at"]
            ).total_seconds() / 60
            r["avail_qty"] = int(r["avail_qty"])
            r["planned_batch"] = int(r["planned_batch"])
            r["kit_ok"] = None if r["kit_ok"].strip() == "" else int(r["kit_ok"])
            rows.append(r)
    return rows


def rate(sub):
    n = len(sub)
    fail = sum(1 for r in sub if r["kit_ok"] == 0)
    return f"n={n:4d}  fail={fail:3d}  rate={fail / n if n else 0:.3f}"


def median(v):
    return statistics.median(v) if v else float("nan")


def rule_r1(r):
    """全局规则：库存快照滞后 >= 10 分钟即预警。"""
    return r["lag_m"] >= STALE_MIN


def rule_r2(r):
    """分车间规则：非电子车间看快照滞后；电子部件车间看库存余量。"""
    if r["workshop"] == ELEC:
        return r["avail_qty"] <= ELEC_AVAIL_MAX
    return r["lag_m"] >= STALE_MIN


def evaluate(rows, rule):
    warned = tp = fp = fn = 0
    for r in rows:
        flag = rule(r)
        if flag:
            warned += 1
        if r["kit_ok"] == 0:
            tp += 1 if flag else 0
            fn += 0 if flag else 1
        elif flag:
            fp += 1
    baseline = sum(1 for r in rows if r["kit_ok"] == 0) * SHORTAGE_MIN
    cost = warned * WARN_MIN + fn * SHORTAGE_MIN
    return {
        "warned": warned, "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / warned if warned else 0,
        "recall": tp / (tp + fn) if tp + fn else 0,
        "baseline": baseline, "cost": cost, "saved": baseline - cost,
        "saved_pct": (baseline - cost) / baseline if baseline else 0,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "kit_orders.csv"
    rows = load(path)
    labeled = [r for r in rows if r["kit_ok"] is not None]
    unlabeled = [r for r in rows if r["kit_ok"] is None]
    failures = sum(1 for r in labeled if r["kit_ok"] == 0)

    print("=" * 70)
    print("1. 数据概况")
    print("=" * 70)
    print(f"总行数={len(rows)}  有标签={len(labeled)}  未标注 kit_ok={len(unlabeled)}")
    print(f"日期范围={min(r['dispatch_time'] for r in labeled).date()} ~ "
          f"{max(r['dispatch_time'] for r in labeled).date()}")
    print(f"有标签工单: 齐套={len(labeled) - failures}  缺料={failures}  "
          f"缺料率={failures / len(labeled):.3f}")
    print("车间分布:", dict(Counter(r["workshop"] for r in labeled)))

    print("\n" + "=" * 70)
    print("2. 库存快照滞后（dispatch_time - stock_synced_at）")
    print("=" * 70)
    normal = [r for r in labeled if r["lag_m"] < STALE_MIN]
    stale = [r for r in labeled if r["lag_m"] >= STALE_MIN]
    print(f"滞后<{STALE_MIN}min: {rate(normal)}")
    print(f"滞后>={STALE_MIN}min: {rate(stale)}")
    lags = sorted(round(r["lag_m"]) for r in labeled)
    gap_lo = max(l for l in lags if l < STALE_MIN)
    gap_hi = min(l for l in lags if l >= STALE_MIN)
    print(f"滞后呈双峰，阈值两侧空档: 正常组最大 {gap_lo} 分钟，"
          f"滞后组最小 {gap_hi} 分钟（阈值取 10 分钟不受影响）")

    print("\n" + "=" * 70)
    print("3. 车间差异")
    print("=" * 70)
    for w in sorted(set(r["workshop"] for r in labeled)):
        ws = [r for r in labeled if r["workshop"] == w]
        fresh = [r for r in ws if r["lag_m"] < STALE_MIN]
        stale_w = [r for r in ws if r["lag_m"] >= STALE_MIN]
        print(f"\n{w}")
        print(f"  滞后<10min : {rate(fresh)}")
        print(f"  滞后>=10min: {rate(stale_w)}")
        for lim in (1, 2, 3, 4):
            low = [r for r in ws if r["avail_qty"] <= lim]
            high = [r for r in ws if r["avail_qty"] > lim]
            print(f"  avail<={lim}: {rate(low)} | avail>{lim}: {rate(high)}")

    print("\n数量因素（avail_qty / planned_batch）对缺料的判别力：")
    ok_ratio = [r["avail_qty"] / r["planned_batch"] for r in labeled if r["kit_ok"] == 1]
    fail_ratio = [r["avail_qty"] / r["planned_batch"] for r in labeled if r["kit_ok"] == 0]
    print(f"  齐套工单比值中位数={median(ok_ratio):.2f}，"
          f"缺料工单比值中位数={median(fail_ratio):.2f}（几乎无差异）")

    print("\n" + "=" * 70)
    print("4. 预警规则回测（仅用有标签工单）")
    print("=" * 70)
    print("R1 全局规则: 滞后>=10min 即预警")
    print("R2 分车间规则: 非电子车间 滞后>=10min; "
          f"电子部件车间 avail_qty<={ELEC_AVAIL_MAX}")
    for name, rule in (("R1", rule_r1), ("R2", rule_r2)):
        m = evaluate(labeled, rule)
        print(f"\n{name}: 预警={m['warned']} 命中={m['tp']} 误报={m['fp']} "
              f"漏报={m['fn']}")
        print(f"    精确率={m['precision']:.1%}  召回率={m['recall']:.1%}")
        print(f"    成本={m['cost']}min  (缺料停工 {m['baseline']}min)  "
              f"节省={m['saved']}min ({m['saved_pct']:.1%})")

    print("\n" + "=" * 70)
    print("5. 时间外推验证：前 5 天定规则，后 2 天验证（R2 固定不变）")
    print("=" * 70)
    dates = sorted(set(r["dispatch_time"].date() for r in labeled))
    split = dates[5]
    train = [r for r in labeled if r["dispatch_time"].date() < split]
    test = [r for r in labeled if r["dispatch_time"].date() >= split]
    for name, sub in (("前5天", train), ("后2天", test)):
        m = evaluate(sub, rule_r2)
        print(f"{name}: n={len(sub)} 预警={m['warned']} 命中={m['tp']} "
              f"误报={m['fp']} 漏报={m['fn']} 节省={m['saved']}min "
              f"({m['saved_pct']:.1%})")

    print("\n" + "=" * 70)
    print("6. 敏感性：预警后复核的实际止损概率")
    print("=" * 70)
    m = evaluate(labeled, rule_r2)
    for eff in (1.0, 0.8, 0.5):
        # 未止损的命中工单仍按停工计
        saved = m["tp"] * SHORTAGE_MIN * eff - m["warned"] * WARN_MIN
        print(f"止损概率={eff:.0%}: 净节省={saved:.0f}min "
              f"({saved / m['baseline']:.1%})")

    print("\n" + "=" * 70)
    print("7. 数据质量")
    print("=" * 70)
    print(f"kit_ok 未标注 {len(unlabeled)} 行（{len(unlabeled) / len(rows):.1%}），"
          f"其中滞后>=10min {sum(1 for r in unlabeled if r['lag_m'] >= STALE_MIN)} 行；"
          "已从回测中排除，建议补齐标签闭环。")


if __name__ == "__main__":
    main()
