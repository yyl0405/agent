#!/usr/bin/env python3
"""kit_guard.py —— 排产下发前的齐套高风险闸门（规则 R2）。

规则（阈值来自 2026-09 的 7 天回测，见 README）：
- 电子部件车间：avail_qty <= 3        -> HOLD（高风险，人工复核/补货）
- 其他车间：库存快照滞后 >= 10 分钟   -> HOLD（账面库存可能过期）
- 其余                                -> RELEASE（按原流程下料）

用法：
    python3 kit_guard.py --input kit_orders.csv
    python3 kit_guard.py --input kit_orders.csv --output flagged.csv
    python3 kit_guard.py --input kit_orders.csv --stale-min 10 --elec-avail-max 3

输出：标准输出打印汇总；给出 --output 时额外写出带判定列的 CSV。
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime

RELEASE = "RELEASE"
HOLD = "HOLD"

# 历史风险估计（仅用于展示优先级，来自 7 天样本：见 README 回测表）
RISK_ESTIMATE = {
    "STALE_SNAPSHOT": 0.50,
    "ELEC_LOW_AVAIL": 0.39,
    "FRESH_SNAPSHOT": 0.014,
    "ELEC_NORMAL": 0.009,
}


@dataclass(frozen=True)
class GateParams:
    stale_min: float = 10.0
    elec_workshop: str = "电子部件车间"
    elec_avail_max: int = 3
    breakeven_risk: float = 20 / 120  # 预警 20min / 停工 120min


DEFAULT_PARAMS = GateParams()


def parse_ts(value):
    """解析 ISO 时间；空值返回 None。兼容 +08:00 与 Z（UTC）。"""
    if value is None or str(value).strip() == "":
        return None
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def snapshot_lag_minutes(dispatch_time, stock_synced_at):
    """库存快照在下发时的年龄（分钟）；任一缺失返回 None。"""
    if dispatch_time is None or stock_synced_at is None:
        return None
    return (dispatch_time - stock_synced_at).total_seconds() / 60


def assess(dispatch_time, stock_synced_at, workshop, avail_qty,
           params: GateParams = DEFAULT_PARAMS):
    """对单条排产记录给出闸门决策。

    返回 dict：decision / reason / lag_minutes / risk_estimate / message
    """
    lag = snapshot_lag_minutes(dispatch_time, stock_synced_at)
    if workshop == params.elec_workshop:
        if avail_qty <= params.elec_avail_max:
            return _result(HOLD, "ELEC_LOW_AVAIL", lag,
                           f"电子部件车间可用库存 {avail_qty} <= "
                           f"{params.elec_avail_max}，低余量风险高")
        return _result(RELEASE, "ELEC_NORMAL", lag,
                       "电子部件车间库存余量正常")

    if lag is None:
        return _result(HOLD, "MISSING_SYNC_TIME", lag,
                       "缺少库存同步时间，无法验证快照新鲜度")
    if lag >= params.stale_min:
        return _result(HOLD, "STALE_SNAPSHOT", lag,
                       f"库存快照已滞后 {lag:.0f} 分钟"
                       f"（阈值 {params.stale_min:.0f} 分钟），需刷新后复核")
    return _result(RELEASE, "FRESH_SNAPSHOT", lag,
                   f"库存快照滞后 {lag:.0f} 分钟，数据新鲜")


def _result(decision, reason, lag, message):
    risk = RISK_ESTIMATE.get(reason)
    return {
        "decision": decision,
        "reason": reason,
        "lag_minutes": None if lag is None else round(lag, 1),
        "risk_estimate": risk,
        "message": message,
    }


def assess_row(row, params: GateParams = DEFAULT_PARAMS):
    """从原始 CSV 行（字符串）解析并评估。"""
    return assess(
        parse_ts(row.get("dispatch_time")),
        parse_ts(row.get("stock_synced_at")),
        (row.get("workshop") or "").strip(),
        int(row["avail_qty"]),
        params,
    )


def process_csv(input_path, output_path=None, params: GateParams = DEFAULT_PARAMS):
    """批量评估 CSV，返回汇总 dict；可选写出带判定列的 CSV。"""
    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []

    results = []
    for row in rows:
        result = assess_row(row, params)
        results.append((row, result))

    summary = {
        "total": len(results),
        "hold": sum(1 for _, r in results if r["decision"] == HOLD),
        "release": sum(1 for _, r in results if r["decision"] == RELEASE),
        "by_reason": {},
        "labeled_failures": None,
        "labeled_tp": None,
        "labeled_fp": None,
    }
    for _, r in results:
        summary["by_reason"][r["reason"]] = summary["by_reason"].get(r["reason"], 0) + 1

    labeled = [(row, r) for row, r in results
               if (row.get("kit_ok") or "").strip() != ""]
    if labeled:
        failures = sum(1 for row, _ in labeled if int(row["kit_ok"]) == 0)
        tp = sum(1 for row, r in labeled
                 if r["decision"] == HOLD and int(row["kit_ok"]) == 0)
        fp = sum(1 for row, r in labeled
                 if r["decision"] == HOLD and int(row["kit_ok"]) == 1)
        summary["labeled_failures"] = failures
        summary["labeled_tp"] = tp
        summary["labeled_fp"] = fp

    if output_path:
        extra = ["risk_decision", "risk_reason", "lag_minutes",
                 "risk_estimate", "risk_message"]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames + extra)
            writer.writeheader()
            for row, result in results:
                writer.writerow({
                    **row,
                    "risk_decision": result["decision"],
                    "risk_reason": result["reason"],
                    "lag_minutes": result["lag_minutes"],
                    "risk_estimate": result["risk_estimate"],
                    "risk_message": result["message"],
                })
    return summary


def main():
    parser = argparse.ArgumentParser(description="排产下发前的齐套高风险闸门")
    parser.add_argument("--input", required=True, help="输入 CSV 路径")
    parser.add_argument("--output", help="输出 CSV 路径（含判定列）")
    parser.add_argument("--stale-min", type=float, default=DEFAULT_PARAMS.stale_min,
                        help="库存快照滞后阈值（分钟），默认 10")
    parser.add_argument("--elec-avail-max", type=int,
                        default=DEFAULT_PARAMS.elec_avail_max,
                        help="电子部件车间低库存阈值，默认 3")
    parser.add_argument("--elec-workshop", default=DEFAULT_PARAMS.elec_workshop,
                        help="电子部件车间名称")
    args = parser.parse_args()

    params = GateParams(stale_min=args.stale_min,
                        elec_workshop=args.elec_workshop,
                        elec_avail_max=args.elec_avail_max)
    summary = process_csv(args.input, args.output, params)

    print(f"记录总数: {summary['total']}")
    print(f"高风险 HOLD: {summary['hold']}  "
          f"放行 RELEASE: {summary['release']}")
    print("原因分布:")
    for reason, count in sorted(summary["by_reason"].items()):
        print(f"  {reason}: {count}")
    if summary["labeled_failures"] is not None:
        tp = summary["labeled_tp"]
        fp = summary["labeled_fp"]
        print(f"有标签记录: 实际缺料 {summary['labeled_failures']}，"
              f"HOLD 命中 {tp}，HOLD 误报 {fp}")
    if args.output:
        print(f"明细已写入: {args.output}")


if __name__ == "__main__":
    main()
