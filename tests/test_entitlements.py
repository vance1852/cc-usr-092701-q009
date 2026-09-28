from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow


class EntitlementCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "医生", "clinician", actor_id=self.owner)["id"]
        self.coord = self.app.create_staff(self.clinic, "前台", "coordinator", actor_id=self.owner)["id"]
        self.auditor = self.app.create_staff(self.clinic, "财务", "auditor", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coord, "ent-01", "陈女士")["id"]
        self.app.entitlements.register_service(self.clinic, self.owner, "RF", "射频", "energy")

    def tearDown(self):
        self.temp.cleanup()

    def grant(self, source="purchase", count=5, key="grt-1", *, expires="2027-09-01T00:00:00+08:00",
              codes=("RF",), actor=None):
        return self.app.entitlements.grant(
            self.clinic, actor or self.coord, self.patient, source, count, list(codes),
            "2026-09-01", expires, "登记", key)

    def appointment(self, key, quantity=1, *, when="2026-10-01T10:00:00+08:00"):
        end = when.replace("10:00", "10:30")
        return self.app.create_appointment(
            self.clinic, self.coord, self.patient, "射频", when, end, key,
            staff_id=self.clinician, service_code="RF", service_quantity=quantity)

    def balance(self):
        return self.app.entitlements.patient_balance(self.clinic, self.coord, self.patient)["services"][0]

    def test_grant_records_source_scope_expiry_and_rule_version(self):
        result = self.grant(count=3)
        self.assertEqual(result["source"], "purchase")
        self.assertEqual(result["scope"], {"service_codes": ["RF"]})
        self.assertEqual(result["rule_version"], "rules-v1")
        self.assertEqual(self.balance(), {"service_code": "RF", "issued": 3, "held": 0,
                                          "in_review": 0, "used": 0, "available": 3})
        # 发放幂等。
        again = self.grant(count=3)
        self.assertTrue(again["replayed"])
        self.assertEqual(self.balance()["issued"], 3)
        # 补偿、转入只能负责人登记；补录不得直接发放。
        with self.assertRaises(Forbidden):
            self.grant(source="compensation", count=1, key="grt-c")
        with self.assertRaises(Forbidden):
            self.grant(source="backfill", count=1, key="grt-b")

    def test_booking_holds_fefo_and_blocks_when_insufficient_or_expired(self):
        near = self.grant(count=2, key="near", expires="2026-10-15T00:00:00+08:00")["id"]
        far = self.grant(count=5, key="far")["id"]
        apt = self.appointment("apt-1", quantity=3)
        holds = self.app.transition_appointment(self.clinic, self.coord, apt["id"], 1, "book")["entitlement_holds"]
        self.assertEqual([(h["grant_id"], h["quantity"]) for h in holds], [(near, 2), (far, 1)])
        self.assertEqual(self.balance()["available"], 4)
        # 不足时整笔确认失败。
        apt2 = self.appointment("apt-2", quantity=5, when="2026-10-02T10:00:00+08:00")
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coord, apt2["id"], 1, "book")
        # 近期批次过期后不能再被新预约占用。
        self.clock.set(datetime(2026, 10, 16, 0, 0, tzinfo=UTC))
        self.app.entitlements.expire_entitlements(self.clinic, self.auditor)
        apt3 = self.appointment("apt-3", quantity=4, when="2026-10-20T10:00:00+08:00")
        holds3 = self.app.transition_appointment(self.clinic, self.coord, apt3["id"], 1, "book")["entitlement_holds"]
        self.assertTrue(all(h["grant_id"] == far for h in holds3))

    def test_cancel_releases_and_no_show_goes_to_finance_review(self):
        self.grant(count=3)
        apt = self.appointment("apt-1", quantity=2)
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 1, "book")
        self.assertEqual(self.balance()["held"], 2)
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 2, "cancel", reason="患者改期")
        self.assertEqual(self.balance()["held"], 0)
        self.assertEqual(self.balance()["available"], 3)

        apt2 = self.appointment("apt-2", quantity=1, when="2026-10-02T10:00:00+08:00")
        self.app.transition_appointment(self.clinic, self.coord, apt2["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coord, apt2["id"], 2, "no_show", reason="未到")
        bal = self.balance()
        self.assertEqual((bal["held"], bal["in_review"], bal["available"]), (0, 1, 2))
        # 前台无权自行决定扣减还是返还。
        review_seq = next(e["sequence"] for e in
                          self.app.entitlements.patient_ledger(self.clinic, self.auditor, self.patient)["entries"]
                          if e["entry_type"] == "review")
        with self.assertRaises(Forbidden):
            self.app.entitlements.resolve_review(self.clinic, self.coord, review_seq, "deduct", "前台自批")
        self.app.entitlements.resolve_review(self.clinic, self.auditor, review_seq, "release", "患者已提前改期证据成立")
        self.assertEqual(self.balance()["available"], 3)
        self.assertEqual(self.balance()["in_review"], 0)

    def test_settlement_requires_signed_encounter_and_distinguishes_dispositions(self):
        self.grant(count=3)
        apt = self.appointment("apt-1", quantity=3)
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 2, "arrive")
        self.clock.set(datetime(2026, 10, 1, 2, 30, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 3, "start")
        # 无签署记录不能核销。
        with self.assertRaises(Conflict):
            self.app.entitlements.settle_appointment(
                self.clinic, self.clinician, apt["id"],
                [{"service_code": "RF", "redeemed": 3}], "尝试核销")
        enc = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["id"]
        for section in ("chief_complaint", "assessment", "plan"):
            version = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["version"]
            self.app.add_encounter_note(self.clinic, self.clinician, enc, section, "内容", expected_version=version)
        version = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["version"]
        self.app.sign_encounter(self.clinic, self.clinician, enc, version)
        # 前台不能核销。
        with self.assertRaises(Forbidden):
            self.app.entitlements.settle_appointment(
                self.clinic, self.coord, apt["id"],
                [{"service_code": "RF", "redeemed": 2, "released": 1}], "履约")
        result = self.app.entitlements.settle_appointment(
            self.clinic, self.clinician, apt["id"],
            [{"service_code": "RF", "redeemed": 2, "released": 1}], "完成2次，1次返还")
        self.assertEqual(result["lines"][0]["redeemed"], 2)
        bal = self.balance()
        self.assertEqual((bal["used"], bal["available"], bal["held"]), (2, 1, 0))
        # 累计结算不能超过预留。
        with self.assertRaises(Conflict):
            self.app.entitlements.settle_appointment(
                self.clinic, self.clinician, apt["id"],
                [{"service_code": "RF", "redeemed": 1}], "重复结算")

    def test_backfill_and_reversal_require_separation_of_duties(self):
        self.grant(count=2)
        apt = self.appointment("apt-1", quantity=1)
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 2, "arrive")
        self.clock.set(datetime(2026, 10, 1, 2, 30, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 3, "start")
        enc = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["id"]
        for section in ("chief_complaint", "assessment", "plan"):
            version = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["version"]
            self.app.add_encounter_note(self.clinic, self.clinician, enc, section, "内容", expected_version=version)
        version = self.app.encounter_for_appointment(self.clinic, self.clinician, apt["id"])["version"]
        self.app.sign_encounter(self.clinic, self.clinician, enc, version)
        self.app.entitlements.settle_appointment(
            self.clinic, self.clinician, apt["id"], [{"service_code": "RF", "redeemed": 1}], "完成")
        redeem_seq = next(e["sequence"] for e in
                          self.app.entitlements.patient_ledger(self.clinic, self.coord, self.patient)["entries"]
                          if e["entry_type"] == "redeem")
        # 冲正申请：理由必填，申请人不能自审，批准前不入账。
        with self.assertRaises(ValidationError):
            self.app.entitlements.request_reversal(self.clinic, self.coord, self.patient, redeem_seq, 1, "错",
                                                   "rev-1")
        adj = self.app.entitlements.request_reversal(
            self.clinic, self.coord, self.patient, redeem_seq, 1, "多核销一次，申请返还", "rev-1")["id"]
        self.assertEqual(self.balance()["used"], 1)
        with self.assertRaises(Forbidden):
            self.app.entitlements.review_adjustment(self.clinic, self.coord, adj, "approved")
        with self.assertRaises(ValidationError):
            self.app.entitlements.review_adjustment(self.clinic, self.auditor, adj, "rejected")
        self.app.entitlements.review_adjustment(self.clinic, self.auditor, adj, "approved", review_note="核实返还")
        self.assertEqual(self.balance()["used"], 0)
        self.assertEqual(self.balance()["available"], 2)
        # 不能超额冲正。
        with self.assertRaises(Conflict):
            self.app.entitlements.request_reversal(
                self.clinic, self.coord, self.patient, redeem_seq, 1, "再次尝试返还同一笔核销", "rev-2")
        # 补录：批准后才产生发放批次。
        bf = self.app.entitlements.request_backfill(
            self.clinic, self.coord, self.patient, 4, ["RF"], "2027-06-01T00:00:00+08:00",
            "老店纸质疗程本迁移历史补录", "bf-1", evidence_ref="PAPER-1")["id"]
        self.assertEqual(self.balance()["available"], 2)
        self.app.entitlements.review_adjustment(self.clinic, self.auditor, bf, "approved", review_note="已核原始收据")
        self.assertEqual(self.balance()["available"], 6)

    def test_ledger_is_append_only_and_traceable_to_every_movement(self):
        grant = self.grant(count=2)["id"]
        apt = self.appointment("apt-1", quantity=1)
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coord, apt["id"], 2, "cancel", reason="改期")
        for statement in ("UPDATE entitlement_ledger SET quantity=9 WHERE sequence=1",
                          "DELETE FROM entitlement_ledger WHERE sequence=1"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.db.transaction() as connection:
                    connection.execute(statement)
        detail = self.app.entitlements.grant_detail(self.clinic, self.coord, grant)
        self.assertEqual([e["entry_type"] for e in detail["entries"]], ["issue", "hold", "release"])
        self.assertTrue(all(e["reason"] for e in detail["entries"]))

    def test_clinic_boundary_is_enforced(self):
        other = self.app.create_clinic("另一诊所", "UTC")
        outsider = self.app.create_staff(other["id"], "负责人", "owner")
        with self.assertRaises(NotFound):
            self.app.entitlements.patient_balance(other["id"], outsider["id"], self.patient)


if __name__ == "__main__":
    unittest.main()
