#!/usr/bin/env python3
"""kit_guard 单元测试与数据集集成测试。

运行：python3 -m unittest -v
"""
import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import kit_guard as kg

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit_orders.csv")


class AssessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = datetime.fromisoformat("2026-09-01T07:00:00+08:00")

    def test_non_elec_stale_boundary(self):
        at_threshold = kg.assess(self.dispatch,
                                 self.dispatch - timedelta(minutes=10),
                                 "一号装配车间", 10)
        self.assertEqual(at_threshold["decision"], kg.HOLD)
        self.assertEqual(at_threshold["reason"], "STALE_SNAPSHOT")

        just_fresh = kg.assess(self.dispatch,
                               self.dispatch - timedelta(minutes=9.99),
                               "一号装配车间", 10)
        self.assertEqual(just_fresh["decision"], kg.RELEASE)
        self.assertEqual(just_fresh["reason"], "FRESH_SNAPSHOT")

    def test_elec_low_avail_holds_regardless_of_lag(self):
        result = kg.assess(self.dispatch,
                           self.dispatch - timedelta(minutes=1),
                           "电子部件车间", 3)
        self.assertEqual(result["decision"], kg.HOLD)
        self.assertEqual(result["reason"], "ELEC_LOW_AVAIL")

    def test_elec_normal_avail_releases_even_when_stale(self):
        result = kg.assess(self.dispatch,
                           self.dispatch - timedelta(minutes=60),
                           "电子部件车间", 4)
        self.assertEqual(result["decision"], kg.RELEASE)
        self.assertEqual(result["reason"], "ELEC_NORMAL")

    def test_missing_sync_time_fails_safe(self):
        result = kg.assess(self.dispatch, None, "重型总装车间", 20)
        self.assertEqual(result["decision"], kg.HOLD)
        self.assertEqual(result["reason"], "MISSING_SYNC_TIME")

    def test_timezone_z_and_offset_are_consistent(self):
        # 2026-08-31T22:53:00Z == 2026-09-01T06:53:00+08:00，滞后 7 分钟
        synced = kg.parse_ts("2026-08-31T22:53:00Z")
        result = kg.assess(self.dispatch, synced, "一号装配车间", 4)
        self.assertAlmostEqual(result["lag_minutes"], 7.0)
        self.assertEqual(result["decision"], kg.RELEASE)

    def test_custom_params(self):
        params = kg.GateParams(stale_min=5, elec_avail_max=1)
        stale_6 = kg.assess(self.dispatch,
                            self.dispatch - timedelta(minutes=6),
                            "一号装配车间", 10, params)
        self.assertEqual(stale_6["decision"], kg.HOLD)
        elec_2 = kg.assess(self.dispatch, self.dispatch, "电子部件车间", 2, params)
        self.assertEqual(elec_2["decision"], kg.RELEASE)


class BatchCsvTests(unittest.TestCase):
    FIELDS = ["order_id", "dispatch_time", "stock_synced_at", "workshop",
              "part_type", "avail_qty", "planned_batch", "kit_ok"]

    def _write_csv(self, rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_process_csv_summary_and_output(self):
        rows = [
            # 非电子、新鲜 -> 放行
            dict(order_id="1", dispatch_time="2026-09-01T07:00:00+08:00",
                 stock_synced_at="2026-08-31T22:55:00Z", workshop="一号装配车间",
                 part_type="伺服电机", avail_qty="10", planned_batch="50", kit_ok="1"),
            # 非电子、滞后 30 分钟、缺料 -> HOLD 命中
            dict(order_id="2", dispatch_time="2026-09-01T08:00:00+08:00",
                 stock_synced_at="2026-08-31T23:30:00Z", workshop="重型总装车间",
                 part_type="精密导轨", avail_qty="20", planned_batch="60", kit_ok="0"),
            # 电子、低库存、缺料 -> HOLD 命中
            dict(order_id="3", dispatch_time="2026-09-01T09:00:00+08:00",
                 stock_synced_at="2026-09-01T00:58:00Z", workshop="电子部件车间",
                 part_type="主控板卡", avail_qty="2", planned_batch="40", kit_ok="0"),
            # 电子、库存充足但快照很旧 -> 放行
            dict(order_id="4", dispatch_time="2026-09-01T10:00:00+08:00",
                 stock_synced_at="2026-08-31T23:00:00Z", workshop="电子部件车间",
                 part_type="主控板卡", avail_qty="10", planned_batch="40", kit_ok="1"),
            # 非电子、滞后、实际齐套 -> HOLD 误报
            dict(order_id="5", dispatch_time="2026-09-01T11:00:00+08:00",
                 stock_synced_at="2026-09-01T02:30:00Z", workshop="精密加工车间",
                 part_type="液压泵阀", avail_qty="15", planned_batch="45", kit_ok="1"),
        ]
        in_path = self._write_csv(rows)
        out_path = in_path + ".out.csv"
        try:
            summary = kg.process_csv(in_path, out_path)
            self.assertEqual(summary["total"], 5)
            self.assertEqual(summary["hold"], 3)
            self.assertEqual(summary["release"], 2)
            self.assertEqual(summary["labeled_failures"], 2)
            self.assertEqual(summary["labeled_tp"], 2)
            self.assertEqual(summary["labeled_fp"], 1)

            with open(out_path, newline="", encoding="utf-8") as f:
                out = list(csv.DictReader(f))
            self.assertEqual(len(out), 5)
            by_id = {r["order_id"]: r for r in out}
            self.assertEqual(by_id["1"]["risk_decision"], kg.RELEASE)
            self.assertEqual(by_id["2"]["risk_decision"], kg.HOLD)
            self.assertEqual(by_id["2"]["risk_reason"], "STALE_SNAPSHOT")
            self.assertEqual(by_id["3"]["risk_reason"], "ELEC_LOW_AVAIL")
            self.assertEqual(by_id["4"]["risk_decision"], kg.RELEASE)
            self.assertEqual(by_id["5"]["risk_reason"], "STALE_SNAPSHOT")
        finally:
            for path in (in_path, out_path):
                if os.path.exists(path):
                    os.unlink(path)

    def test_production_rule_matches_analysis_on_full_dataset(self):
        """生产闸门在原始数据上的判定必须与 README/analyze.py 的回测一致。"""
        self.assertTrue(os.path.exists(DATA), "kit_orders.csv 缺失")
        summary = kg.process_csv(DATA)
        self.assertEqual(summary["total"], 1036)
        # 全量 190 条 HOLD：其中 185 条有标签、5 条未标注
        self.assertEqual(summary["hold"], 190)
        self.assertEqual(summary["release"], 846)
        self.assertEqual(summary["labeled_failures"], 100)
        self.assertEqual(summary["labeled_tp"], 90)
        self.assertEqual(summary["labeled_fp"], 95)


if __name__ == "__main__":
    unittest.main()
