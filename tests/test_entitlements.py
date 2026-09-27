from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, Unauthorized, ValidationError
from careflow.service import Careflow


class EntitlementCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "前台协调", "coordinator", actor_id=self.owner)["id"]
        self.auditor = self.app.create_staff(self.clinic, "财务稽核", "auditor", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-101", "王女士")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def grant(self, sessions=10, item="光子嫩肤", source_type="purchase", key="g-1",
              source_ref="ORDER-1", **kwargs):
        return self.app.entitlements.grant(self.clinic, self.coordinator, self.patient, item, sessions,
                                           source_type, source_ref, key, **kwargs)

    def appointment(self, key="visit-1", kind="复诊", starts="2026-09-29T10:00:00+08:00",
                    ends="2026-09-29T10:30:00+08:00"):
        return self.app.create_appointment(self.clinic, self.coordinator, self.patient, kind,
                                           starts, ends, key, staff_id=self.clinician)

    def sign_for(self, appointment):
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "start")
        encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        for section in ("chief_complaint", "assessment", "plan"):
            self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], section, f"记录-{section}",
                                        expected_version=encounter["version"])
            encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], encounter["version"])
        return encounter

    def totals(self, item="光子嫩肤"):
        balance = self.app.entitlements.balance(self.clinic, self.coordinator, self.patient)
        for bucket in balance["items"]:
            if bucket["item_code"] == item:
                return bucket["totals"]
        return None

    def ledger(self):
        return self.app.entitlements.ledger(self.clinic, self.coordinator, self.patient)

    def test_grant_records_source_validity_and_rule_version(self):
        first = self.grant(10, key="g-1", valid_until="2026-12-31", rule_version=3, source_ref="ORDER-100")
        replay = self.grant(10, key="g-1", valid_until="2026-12-31", rule_version=3, source_ref="ORDER-100")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["id"], replay["id"])
        gift = self.grant(2, source_type="gift", key="g-2", source_ref="GIFT-9")
        balance = self.app.entitlements.balance(self.clinic, self.auditor, self.patient)
        item = balance["items"][0]
        self.assertEqual(item["totals"]["granted"], 12)
        self.assertEqual(item["totals"]["available"], 12)
        by_id = {row["id"]: row for row in item["grants"]}
        self.assertEqual(by_id[first["id"]]["rule_version"], 3)
        self.assertEqual(by_id[first["id"]]["valid_until"], "2026-12-31")
        self.assertEqual(by_id[gift["id"]]["source_ref"], "GIFT-9")
        ledger = self.ledger()
        self.assertEqual({entry["entry_type"] for entry in ledger}, {"grant"})
        self.assertTrue(all(entry["source_ref"] for entry in ledger))
        with self.assertRaises(ValidationError):
            self.grant(1, key="g-3", valid_from="2026-12-01", valid_until="2026-11-01")

    def test_reserve_allocates_earliest_expiry_first_and_is_atomic(self):
        later = self.grant(5, key="g-late", valid_until="2027-03-31", source_ref="O-L")
        earlier = self.grant(5, key="g-early", valid_until="2026-10-31", source_ref="O-E")
        appointment = self.appointment()
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 6, "res-1")
        self.assertEqual([(row["grant_id"], row["sessions"]) for row in reserved["reservations"]],
                         [(earlier["id"], 5), (later["id"], 1)])
        replay = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 6, "res-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(reserved["reservations"], replay["reservations"])
        with self.assertRaises(Conflict):
            self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 5, "res-2")
        self.assertEqual(self.totals()["available"], 4)
        self.assertEqual(self.totals()["held"], 6)

    def test_expired_or_out_of_scope_grant_cannot_be_reserved(self):
        self.grant(4, key="g-exp", valid_from="2026-09-01", valid_until="2026-09-28")
        appointment = self.appointment()
        # 预约日为 2026-09-29，权益 2026-09-28 已失效
        with self.assertRaises(Conflict) as expired:
            self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 1, "res-exp")
        self.assertIn("过期", expired.exception.message)
        self.grant(4, key="g-scope", valid_until="2026-12-31", scope={"appointment_kinds": ["咨询"]})
        with self.assertRaises(Conflict):
            self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 1, "res-scope")
        consult = self.appointment("visit-consult", kind="咨询",
                                   starts="2026-09-29T11:00:00+08:00", ends="2026-09-29T11:30:00+08:00")
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, consult["id"], "光子嫩肤", 1, "res-ok")
        self.assertEqual(reserved["sessions"], 1)

    def test_appointment_cancel_releases_and_no_show_goes_to_review(self):
        self.grant(4, key="g-1", valid_until="2026-12-31")
        first = self.appointment("visit-a")
        self.app.entitlements.reserve(self.clinic, self.coordinator, first["id"], "光子嫩肤", 2, "res-a")
        self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "cancel", reason="患者改期")
        self.assertEqual(self.totals()["available"], 4)
        self.assertEqual(self.totals()["held"], 0)
        release = [entry for entry in self.ledger() if entry["entry_type"] == "release"]
        self.assertEqual(release[0]["appointment_id"], first["id"])
        second = self.appointment("visit-b")
        self.app.transition_appointment(self.clinic, self.coordinator, second["id"], 1, "book")
        self.app.entitlements.reserve(self.clinic, self.coordinator, second["id"], "光子嫩肤", 2, "res-b")
        self.app.transition_appointment(self.clinic, self.coordinator, second["id"], 2, "no_show", reason="未到店")
        self.assertEqual(self.totals()["pending_review"], 2)
        self.assertEqual(self.totals()["available"], 2)
        reviews = self.app.entitlements.pending_reviews(self.clinic, self.owner)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["review_sessions"], 2)
        with self.assertRaises(Forbidden):
            self.app.entitlements.resolve_review(self.clinic, self.coordinator, reviews[0]["id"],
                                                 "deduct", "前台自行扣减", reviews[0]["version"])
        resolved = self.app.entitlements.resolve_review(self.clinic, self.owner, reviews[0]["id"],
                                                        "deduct", "爽约按规则版本扣减", reviews[0]["version"])
        self.assertEqual(resolved["state"], "review_deducted")
        self.assertEqual(self.totals()["consumed"], 2)
        self.assertEqual(self.totals()["pending_review"], 0)

    def test_hold_expiry_releases_reserved_sessions(self):
        self.grant(2, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 2, "res-1")
        self.clock.set(datetime(2026, 9, 27, 12, 11, tzinfo=UTC))
        self.assertEqual(self.app.expire_holds(self.clinic)["expired"], 1)
        self.assertEqual(self.totals()["available"], 2)
        reasons = [entry["reason"] for entry in self.ledger() if entry["entry_type"] == "release"]
        self.assertTrue(any("占位过期" in reason for reason in reasons))

    def test_consume_requires_signed_encounter_and_splits_partial(self):
        self.grant(5, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 3, "res-1")
        reservation_id = reserved["reservations"][0]["id"]
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "start")
        with self.assertRaises(Conflict):
            self.app.entitlements.consume(self.clinic, self.clinician, reservation_id, 2, 1, remainder="release")
        encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        for section in ("chief_complaint", "assessment", "plan"):
            self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], section, f"记录-{section}",
                                        expected_version=encounter["version"])
            encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], encounter["version"])
        with self.assertRaises(ValidationError):
            self.app.entitlements.consume(self.clinic, self.clinician, reservation_id, 2, 1)
        result = self.app.entitlements.consume(self.clinic, self.clinician, reservation_id, 2, 1, remainder="release")
        self.assertEqual(result["state"], "consumed")
        self.assertEqual(result["returned_sessions"], 1)
        totals = self.totals()
        self.assertEqual(totals["consumed"], 2)
        self.assertEqual(totals["available"], 3)
        consume = [entry for entry in self.ledger() if entry["entry_type"] == "consume"][0]
        self.assertEqual(consume["encounter_id"], encounter["id"])
        self.assertEqual(consume["sessions"], 2)

    def test_partial_consume_can_route_remainder_to_finance_review(self):
        self.grant(5, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 3, "res-1")
        reservation_id = reserved["reservations"][0]["id"]
        self.sign_for(appointment)
        result = self.app.entitlements.consume(self.clinic, self.clinician, reservation_id, 1, 1, remainder="review")
        self.assertEqual(result["state"], "pending_review")
        self.assertEqual(result["review_sessions"], 2)
        reviews = self.app.entitlements.pending_reviews(self.clinic, self.auditor)
        self.assertEqual(len(reviews), 1)
        resolved = self.app.entitlements.resolve_review(self.clinic, self.owner, reservation_id,
                                                        "release", "剩余项目未做，返还患者", reviews[0]["version"])
        self.assertEqual(resolved["state"], "review_released")
        totals = self.totals()
        self.assertEqual(totals["consumed"], 1)
        self.assertEqual(totals["available"], 4)

    def test_supplement_and_reverse_require_reason_and_owner(self):
        granted = self.grant(5, key="g-1", valid_until="2026-12-31")
        with self.assertRaises(Forbidden):
            self.app.entitlements.supplement(self.clinic, self.coordinator, granted["id"], 2, "前台补录", "sup-1")
        supplemented = self.app.entitlements.supplement(self.clinic, self.owner, granted["id"], 2,
                                                        "线下收款补录", "sup-1")
        self.assertEqual(self.totals()["available"], 7)
        entry = [row for row in self.ledger() if row["id"] == supplemented["entry_id"]][0]
        self.assertEqual(entry["reason"], "线下收款补录")
        with self.assertRaises(Forbidden):
            self.app.entitlements.reverse(self.clinic, self.coordinator, supplemented["entry_id"], "冲正", "rev-1")
        reversed_entry = self.app.entitlements.reverse(self.clinic, self.owner, supplemented["entry_id"],
                                                       "补录金额错误", "rev-1")
        self.assertEqual(reversed_entry["delta"], -2)
        self.assertEqual(self.totals()["available"], 5)
        with self.assertRaises(Conflict):
            self.app.entitlements.reverse(self.clinic, self.owner, supplemented["entry_id"], "再次冲正", "rev-2")

    def test_consume_reverse_restores_sessions_and_grant_reverse_needs_balance(self):
        granted = self.grant(3, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 2, "res-1")
        self.sign_for(appointment)
        self.app.entitlements.consume(self.clinic, self.clinician, reserved["reservations"][0]["id"], 2, 1)
        self.assertEqual(self.totals()["available"], 1)
        grant_entry = [row for row in self.ledger() if row["entry_type"] == "grant"][0]
        with self.assertRaises(Conflict):
            self.app.entitlements.reverse(self.clinic, self.owner, grant_entry["id"], "直接冲正发放", "rev-g")
        consume_entry = [row for row in self.ledger() if row["entry_type"] == "consume"][0]
        self.app.entitlements.reverse(self.clinic, self.owner, consume_entry["id"], "患者投诉复核返还", "rev-c")
        self.assertEqual(self.totals()["available"], 3)
        self.app.entitlements.reverse(self.clinic, self.owner, grant_entry["id"], "重复开单作废", "rev-g2")
        balance = self.app.entitlements.balance(self.clinic, self.owner, self.patient)
        self.assertEqual(balance["items"][0]["grants"][0]["state"], "void")
        self.assertEqual(self.totals()["available"], 0)

    def test_expire_sweep_blocks_further_reservations(self):
        self.grant(4, key="g-1", valid_from="2026-09-01", valid_until="2026-09-27")
        self.clock.set(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
        result = self.app.entitlements.expire_grants(self.clinic)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.app.entitlements.expire_grants(self.clinic)["expired"], 0)
        totals = self.totals()
        self.assertEqual(totals["expired"], 4)
        self.assertEqual(totals["available"], 0)
        appointment = self.appointment()
        with self.assertRaises(Conflict):
            self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 1, "res-1")

    def test_settlement_waits_for_review_and_locks_snapshot(self):
        self.grant(6, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        reserved = self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 2, "res-1")
        self.sign_for(appointment)
        self.app.entitlements.consume(self.clinic, self.clinician, reserved["reservations"][0]["id"], 1, 1, remainder="review")
        with self.assertRaises(Conflict):
            self.app.entitlements.close_settlement(self.clinic, self.owner, "2026-09-01", "2026-09-30",
                                                   "九月结账", "set-1")
        reviews = self.app.entitlements.pending_reviews(self.clinic, self.owner)
        self.app.entitlements.resolve_review(self.clinic, self.owner, reviews[0]["id"],
                                             "deduct", "已做部分按次扣减", reviews[0]["version"])
        with self.assertRaises(Forbidden):
            self.app.entitlements.close_settlement(self.clinic, self.coordinator, "2026-09-01", "2026-09-30",
                                                   "前台尝试结账", "set-x")
        closed = self.app.entitlements.close_settlement(self.clinic, self.owner, "2026-09-01", "2026-09-30",
                                                        "九月结账", "set-1")
        self.assertEqual(closed["totals"]["光子嫩肤"]["consumed"], 2)
        self.assertEqual(closed["totals"]["光子嫩肤"]["available"], 4)
        replay = self.app.entitlements.close_settlement(self.clinic, self.owner, "2026-09-01", "2026-09-30",
                                                        "九月结账", "set-1")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.app.entitlements.close_settlement(self.clinic, self.owner, "2026-09-15", "2026-10-15",
                                                   "重叠期间", "set-2")
        consume_entry = [row for row in self.ledger() if row["entry_type"] == "consume"][0]
        reversed_entry = self.app.entitlements.reverse(self.clinic, self.owner, consume_entry["id"],
                                                       "患者投诉复核返还", "rev-9")
        self.assertTrue(reversed_entry["reversed_settled_entry"])
        detail = self.app.entitlements.settlement_detail(self.clinic, self.auditor, closed["id"])
        self.assertEqual(detail["totals"]["光子嫩肤"]["consumed"], 2)
        self.assertEqual(self.totals()["available"], 5)

    def test_diagnostics_flags_dangling_reservation(self):
        self.grant(2, key="g-1", valid_until="2026-12-31")
        appointment = self.appointment()
        self.app.entitlements.reserve(self.clinic, self.coordinator, appointment["id"], "光子嫩肤", 1, "res-1")
        self.sign_for(appointment)
        self.clock.set(datetime(2026, 9, 29, 2, 31, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 4, "complete")
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("entitlement.reservation.unresolved", {item["code"] for item in report["findings"]})

    def test_http_entitlement_routes(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/auth/token", data=json.dumps({"staff_id": self.owner,
                              "password": "LongPassphrase!2026"}).encode(), method="POST",
                              headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                token = json.loads(response.read())["access_token"]
            headers = {"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}
            request = Request(base + f"/patients/{self.patient}/entitlements",
                              data=json.dumps({"item_code": "水光针", "sessions": 6, "source_type": "purchase",
                                               "source_ref": "ORDER-HTTP", "valid_until": "2026-12-31",
                                               "rule_version": 2}).encode(), method="POST",
                              headers={**headers, "Idempotency-Key": "http-grant-1"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 201)
            request = Request(base + f"/patients/{self.patient}/entitlements", headers=headers)
            with urlopen(request, timeout=3) as response:
                balance = json.loads(response.read())
            self.assertEqual(balance["items"][0]["totals"]["available"], 6)
            request = Request(base + f"/patients/{self.patient}/entitlement-ledger", headers=headers)
            with urlopen(request, timeout=3) as response:
                ledger = json.loads(response.read())
            self.assertEqual(ledger["items"][0]["entry_type"], "grant")
            request = Request(base + f"/patients/{self.patient}/entitlements",
                              data=json.dumps({"item_code": "水光针", "sessions": 1, "source_type": "purchase",
                                               "source_ref": "ORDER-HTTP"}).encode(), method="POST",
                              headers={**headers, "Idempotency-Key": "http-grant-1"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
