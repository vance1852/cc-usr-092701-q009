"""患者次数权益台账：发放、预约占用、签署核销、财务复核与月末结账。

台账分录只追加不修改：余额由分录求和得出，预留先扣可用、释放返还、核销记零增量。
已结账期间的历史不会被后台直接改数，更正一律通过补录或冲正分录落在当前期间。
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import (
    calendar_date,
    choice,
    integer,
    object_value,
    parsed_timestamp,
    request_digest,
    require_match,
    text,
    timestamp,
)

GRANT_SOURCES = {"purchase", "gift", "transfer_in"}
SOURCE_LABELS = {"purchase": "购买", "gift": "赠送", "transfer_in": "转店转入"}
REVERSIBLE_TYPES = {"grant", "supplement", "consume", "review_deduct"}


class EntitlementService:
    """次数权益按患者与项目分账；前台与财务读取同一份台账。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    def local_date(self, connection, clinic_id: str) -> str:
        row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return self.clock.now().astimezone(ZoneInfo(row["timezone"])).date().isoformat()

    @staticmethod
    def _date_of(timestamp_text: str, timezone: str) -> str:
        return parsed_timestamp(timestamp_text).astimezone(ZoneInfo(timezone)).date().isoformat()

    @staticmethod
    def _normalize_scope(scope: Any) -> str:
        scope = object_value(scope if scope is not None else {}, "适用范围", allowed={"appointment_kinds"})
        kinds = scope.get("appointment_kinds", [])
        if not isinstance(kinds, list) or len(kinds) > 20:
            raise ValidationError("适用预约类型列表无效")
        normalized = sorted({text(kind, "适用预约类型", maximum=100) for kind in kinds})
        return encode_json({"appointment_kinds": normalized})

    def _entry(self, connection, *, grant_id: str, clinic_id: str, patient_id: str, item_code: str,
               entry_type: str, sessions: int, delta: int, actor_id: str | None, reason: str,
               key: str, now: str, appointment_id: str | None = None, reservation_id: str | None = None,
               encounter_id: str | None = None, reverses: str | None = None) -> str:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM entitlement_entries WHERE grant_id=?", (grant_id,)).fetchone()[0]
        entry_id = new_id("een")
        connection.execute(
            "INSERT INTO entitlement_entries(id,grant_id,clinic_id,patient_id,item_code,entry_type,sessions,delta,"
            "appointment_id,reservation_id,encounter_id,reverses,actor_id,reason,idempotency_key,created_at,sequence) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, grant_id, clinic_id, patient_id, item_code, entry_type, sessions, delta,
             appointment_id, reservation_id, encounter_id, reverses, actor_id, reason, key, now, sequence))
        return entry_id

    @staticmethod
    def _available(connection, grant_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(delta),0) FROM entitlement_entries WHERE grant_id=?", (grant_id,)).fetchone()[0]

    def _breakdown(self, connection, grant_id: str) -> dict[str, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type IN ('grant','supplement') THEN sessions ELSE 0 END),0) AS granted,"
            "COALESCE(SUM(CASE WHEN entry_type IN ('consume','review_deduct') THEN sessions ELSE 0 END),0) AS consumed,"
            "COALESCE(SUM(CASE WHEN entry_type='expire' THEN sessions ELSE 0 END),0) AS expired,"
            "COALESCE(SUM(CASE WHEN entry_type='reverse' AND delta<0 THEN sessions ELSE 0 END),0) AS reversed_out,"
            "COALESCE(SUM(CASE WHEN entry_type='reverse' AND delta>0 THEN sessions ELSE 0 END),0) AS returned,"
            "COALESCE(SUM(delta),0) AS available FROM entitlement_entries WHERE grant_id=?", (grant_id,)).fetchone()
        held = connection.execute(
            "SELECT COALESCE(SUM(sessions),0) FROM entitlement_reservations WHERE grant_id=? AND state='reserved'",
            (grant_id,)).fetchone()[0]
        review = connection.execute(
            "SELECT COALESCE(SUM(sessions-consumed_sessions),0) FROM entitlement_reservations "
            "WHERE grant_id=? AND state='pending_review'", (grant_id,)).fetchone()[0]
        return {"granted": row["granted"], "consumed": row["consumed"], "expired": row["expired"],
                "reversed_out": row["reversed_out"], "returned": row["returned"],
                "held": held, "pending_review": review, "available": row["available"]}

    def _refresh_grant_state(self, connection, grant_id: str) -> None:
        grant = connection.execute("SELECT state FROM entitlement_grants WHERE id=?", (grant_id,)).fetchone()
        if grant["state"] != "active" or self._available(connection, grant_id) > 0:
            return
        open_holds = connection.execute(
            "SELECT COUNT(*) FROM entitlement_reservations WHERE grant_id=? AND state IN ('reserved','pending_review')",
            (grant_id,)).fetchone()[0]
        if open_holds:
            return
        consumed = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type IN ('consume','review_deduct') THEN sessions "
            "WHEN entry_type='reverse' AND delta>0 THEN -sessions ELSE 0 END),0) "
            "FROM entitlement_entries WHERE grant_id=?", (grant_id,)).fetchone()[0]
        state = "exhausted" if consumed > 0 else "void"
        connection.execute("UPDATE entitlement_grants SET state=?,version=version+1 WHERE id=? AND state='active'",
                           (state, grant_id))

    @staticmethod
    def _settled_watermarks(connection, clinic_id: str) -> dict[str, int]:
        row = connection.execute(
            "SELECT snapshot_json FROM entitlement_settlements WHERE clinic_id=? ORDER BY closed_at DESC,id DESC LIMIT 1",
            (clinic_id,)).fetchone()
        if row is None:
            return {}
        return {item["grant_id"]: item["entry_sequence"] for item in decode_json(row["snapshot_json"])["grants"]}

    def grant(self, clinic_id: str, actor_id: str, patient_id: str, item_code: str, sessions: Any,
              source_type: str, source_ref: str, idempotency_key: str, *, valid_from: str | None = None,
              valid_until: str | None = None, rule_version: Any = 1, scope: Any = None) -> dict[str, Any]:
        item_code = text(item_code, "项目编码", maximum=80)
        source_type = choice(source_type, "额度来源", GRANT_SOURCES)
        source_ref = text(source_ref, "来源单号", maximum=120)
        sessions = integer(sessions, "发放次数", minimum=1, maximum=10000)
        rule_version = integer(rule_version, "规则版本", minimum=1, maximum=9999)
        key = require_idempotency_key(idempotency_key)
        scope_json = self._normalize_scope(scope)
        now = self.now()
        request_hash = request_digest({"clinic_id": clinic_id, "patient_id": patient_id, "item_code": item_code,
                                       "sessions": sessions, "source_type": source_type, "source_ref": source_ref,
                                       "valid_from": valid_from, "valid_until": valid_until,
                                       "rule_version": rule_version, "scope": scope_json})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:write", clinic_id=clinic_id)
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_grant' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("发放幂等编号已用于其他内容")
                return {**decode_json(previous["response_json"]), "replayed": True}
            patient = connection.execute("SELECT state FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能发放权益")
            start = calendar_date(valid_from, "生效日期") if valid_from else self.local_date(connection, clinic_id)
            until = calendar_date(valid_until, "失效日期") if valid_until else None
            if until and until < start:
                raise ValidationError("失效日期不能早于生效日期")
            grant_id = new_id("egr")
            connection.execute(
                "INSERT INTO entitlement_grants(id,clinic_id,patient_id,item_code,source_type,source_ref,total_sessions,"
                "scope_json,valid_from,valid_until,rule_version,state,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                (grant_id, clinic_id, patient_id, item_code, source_type, source_ref, sessions, scope_json,
                 start, until, rule_version, actor_id, now))
            entry_id = self._entry(connection, grant_id=grant_id, clinic_id=clinic_id, patient_id=patient_id,
                                   item_code=item_code, entry_type="grant", sessions=sessions, delta=sessions,
                                   actor_id=actor_id, reason=f"{SOURCE_LABELS[source_type]}发放；单号={source_ref}",
                                   key=f"grant:{key}", now=now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="entitlement_grant", aggregate_id=grant_id, action="entitlement.granted",
                               occurred_at=now, payload={"item_code": item_code, "sessions": sessions,
                                                         "source_type": source_type, "source_ref": source_ref,
                                                         "rule_version": rule_version, "valid_from": start,
                                                         "valid_until": until, "entry_id": entry_id})
            result = {"id": grant_id, "patient_id": patient_id, "item_code": item_code, "source_type": source_type,
                      "source_ref": source_ref, "total_sessions": sessions, "rule_version": rule_version,
                      "valid_from": start, "valid_until": until, "scope": decode_json(scope_json),
                      "state": "active", "created_at": now}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("entitlement_grant", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    def reserve(self, clinic_id: str, actor_id: str, appointment_id: str, item_code: str,
                sessions: Any, idempotency_key: str) -> dict[str, Any]:
        item_code = text(item_code, "项目编码", maximum=80)
        sessions = integer(sessions, "占用次数", minimum=1, maximum=1000)
        key = require_idempotency_key(idempotency_key)
        now = self.now()
        request_hash = request_digest({"clinic_id": clinic_id, "appointment_id": appointment_id,
                                       "item_code": item_code, "sessions": sessions})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:write", clinic_id=clinic_id)
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_reserve' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("权益占用幂等编号已用于其他内容")
                return {**decode_json(previous["response_json"]), "replayed": True}
            appointment = connection.execute("SELECT * FROM appointments WHERE id=? AND clinic_id=?",
                                             (appointment_id, clinic_id)).fetchone()
            if appointment is None:
                raise NotFound("预约不存在")
            if appointment["state"] not in {"held", "booked", "arrived"}:
                raise Conflict("当前预约状态不能占用次数权益")
            patient = connection.execute("SELECT state FROM patients WHERE id=?", (appointment["patient_id"],)).fetchone()
            if patient is None or patient["state"] != "active":
                raise Conflict("非在诊患者不能占用权益")
            timezone = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()["timezone"]
            visit_date = self._date_of(appointment["starts_at"], timezone)
            rows = connection.execute(
                "SELECT g.*,COALESCE(SUM(e.delta),0) AS available FROM entitlement_grants g "
                "LEFT JOIN entitlement_entries e ON e.grant_id=g.id "
                "WHERE g.clinic_id=? AND g.patient_id=? AND g.item_code=? AND g.state='active' "
                "GROUP BY g.id ORDER BY CASE WHEN g.valid_until IS NULL THEN 1 ELSE 0 END,g.valid_until,g.created_at,g.id",
                (clinic_id, appointment["patient_id"], item_code)).fetchall()
            eligible = []
            skipped_expired = 0
            for row in rows:
                if row["valid_until"] and row["valid_until"] < visit_date:
                    skipped_expired += 1
                    continue
                if row["valid_from"] > visit_date:
                    continue
                kinds = decode_json(row["scope_json"]).get("appointment_kinds", [])
                if kinds and appointment["kind"] not in kinds:
                    continue
                if row["available"] <= 0:
                    continue
                eligible.append(row)
            if not eligible:
                if rows and skipped_expired == len(rows):
                    raise Conflict("权益已过期，不能被新预约占用")
                raise Conflict("没有适用于该预约日期或类型的有效权益")
            remaining = sessions
            plan = []
            for row in eligible:
                take = min(int(row["available"]), remaining)
                plan.append((row, take))
                remaining -= take
                if remaining == 0:
                    break
            if remaining > 0:
                raise Conflict("可用次数权益不足", details={"item_code": item_code, "requested": sessions,
                                                           "available": sessions - remaining})
            reservations = []
            for row, take in plan:
                reservation_id = new_id("ers")
                connection.execute(
                    "INSERT INTO entitlement_reservations(id,clinic_id,grant_id,patient_id,appointment_id,sessions,"
                    "consumed_sessions,state,idempotency_key,reserved_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,0,'reserved',?,?,?,?)",
                    (reservation_id, clinic_id, row["id"], appointment["patient_id"], appointment_id, take,
                     f"{key}:{row['id']}", actor_id, now, now))
                self._entry(connection, grant_id=row["id"], clinic_id=clinic_id, patient_id=appointment["patient_id"],
                            item_code=item_code, entry_type="reserve", sessions=take, delta=-take,
                            actor_id=actor_id, reason="预约确认占用", key=f"reserve:{key}:{row['id']}", now=now,
                            appointment_id=appointment_id, reservation_id=reservation_id)
                reservations.append({"id": reservation_id, "grant_id": row["id"], "sessions": take,
                                     "valid_until": row["valid_until"], "rule_version": row["rule_version"]})
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=appointment["patient_id"],
                               aggregate_type="appointment", aggregate_id=appointment_id, action="entitlement.reserved",
                               occurred_at=now, payload={"item_code": item_code, "sessions": sessions,
                                                         "reservations": reservations, "idempotency_key": key})
            result = {"appointment_id": appointment_id, "patient_id": appointment["patient_id"],
                      "item_code": item_code, "sessions": sessions, "reservations": reservations}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("entitlement_reserve", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    def _reservation(self, connection, clinic_id: str, reservation_id: str):
        return connection.execute(
            "SELECT r.*,a.state AS appointment_state,a.starts_at,g.item_code,g.valid_until "
            "FROM entitlement_reservations r JOIN appointments a ON a.id=r.appointment_id "
            "JOIN entitlement_grants g ON g.id=r.grant_id WHERE r.id=? AND r.clinic_id=?",
            (reservation_id, clinic_id)).fetchone()

    def consume(self, clinic_id: str, actor_id: str, reservation_id: str, consumed_sessions: Any,
                expected_version: int, *, remainder: str | None = None) -> dict[str, Any]:
        consumed_sessions = integer(consumed_sessions, "核销次数", minimum=1, maximum=1000)
        if remainder is not None:
            remainder = choice(remainder, "剩余次数处置", {"release", "review"})
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:write", clinic_id=clinic_id)
            row = self._reservation(connection, clinic_id, reservation_id)
            if row is None:
                raise NotFound("权益占用记录不存在")
            require_match(row["version"], expected_version, "权益占用记录")
            if row["state"] != "reserved":
                raise Conflict("该占用已处理，不能重复核销")
            if consumed_sessions > row["sessions"]:
                raise ValidationError("核销次数不能超过占用次数")
            rest = row["sessions"] - consumed_sessions
            if rest > 0 and remainder is None:
                raise ValidationError("部分履约时必须明确剩余次数处置：release 返还或 review 待财务复核")
            if rest == 0 and remainder is not None:
                raise ValidationError("无剩余次数时不能指定剩余处置")
            if row["appointment_state"] not in {"arrived", "in_service", "completed"}:
                raise Conflict("预约未到服务阶段，不能核销次数")
            encounter = connection.execute("SELECT id,state FROM encounters WHERE appointment_id=?",
                                           (row["appointment_id"],)).fetchone()
            if encounter is None or encounter["state"] not in {"signed", "amended"}:
                raise Conflict("服务签署后才能按实际完成内容核销")
            timezone = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()["timezone"]
            if row["valid_until"] and self._date_of(row["starts_at"], timezone) > row["valid_until"]:
                raise Conflict("权益在预约日期前已过期，请转财务复核处理")
            self._entry(connection, grant_id=row["grant_id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                        item_code=row["item_code"], entry_type="consume", sessions=consumed_sessions, delta=0,
                        actor_id=actor_id, reason="服务签署后按实际完成核销", key=f"consume:{reservation_id}", now=now,
                        appointment_id=row["appointment_id"], reservation_id=reservation_id, encounter_id=encounter["id"])
            state, returned, review = "consumed", 0, 0
            if rest > 0 and remainder == "release":
                returned = rest
                self._entry(connection, grant_id=row["grant_id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                            item_code=row["item_code"], entry_type="release", sessions=rest, delta=rest,
                            actor_id=actor_id, reason="部分履约，剩余次数返还", key=f"consume-release:{reservation_id}",
                            now=now, appointment_id=row["appointment_id"], reservation_id=reservation_id)
            elif rest > 0:
                review = rest
                state = "pending_review"
                self._entry(connection, grant_id=row["grant_id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                            item_code=row["item_code"], entry_type="review_pending", sessions=rest, delta=0,
                            actor_id=actor_id, reason="部分履约，剩余次数待财务复核", key=f"consume-review:{reservation_id}",
                            now=now, appointment_id=row["appointment_id"], reservation_id=reservation_id)
            connection.execute(
                "UPDATE entitlement_reservations SET state=?,consumed_sessions=?,updated_at=?,version=version+1 WHERE id=?",
                (state, consumed_sessions, now, reservation_id))
            self._refresh_grant_state(connection, row["grant_id"])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_reservation", aggregate_id=reservation_id,
                               action="entitlement.consumed", occurred_at=now,
                               payload={"sessions": consumed_sessions, "returned_sessions": returned,
                                        "review_sessions": review, "encounter_id": encounter["id"],
                                        "version": expected_version + 1})
        return {"id": reservation_id, "state": state, "consumed_sessions": consumed_sessions,
                "returned_sessions": returned, "review_sessions": review, "version": expected_version + 1}

    def release(self, clinic_id: str, actor_id: str, reservation_id: str,
                reason: str, expected_version: int) -> dict[str, Any]:
        reason = text(reason, "释放原因", maximum=600)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:write", clinic_id=clinic_id)
            row = self._reservation(connection, clinic_id, reservation_id)
            if row is None:
                raise NotFound("权益占用记录不存在")
            require_match(row["version"], expected_version, "权益占用记录")
            if row["state"] != "reserved":
                raise Conflict("只有待履约的占用可以释放")
            self._release_row(connection, row, reason, now, actor_id)
        return {"id": reservation_id, "state": "released", "sessions": row["sessions"],
                "version": expected_version + 1}

    def _release_row(self, connection, row, reason: str, now: str, actor_id: str | None) -> None:
        self._entry(connection, grant_id=row["grant_id"], clinic_id=row["clinic_id"], patient_id=row["patient_id"],
                    item_code=row["item_code"], entry_type="release", sessions=row["sessions"], delta=row["sessions"],
                    actor_id=actor_id, reason=reason, key=f"release:{row['id']}", now=now,
                    appointment_id=row["appointment_id"], reservation_id=row["id"])
        connection.execute("UPDATE entitlement_reservations SET state='released',updated_at=?,version=version+1 WHERE id=?",
                           (now, row["id"]))
        audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                           aggregate_type="entitlement_reservation", aggregate_id=row["id"],
                           action="entitlement.released", occurred_at=now,
                           payload={"sessions": row["sessions"], "reason": reason,
                                    "appointment_id": row["appointment_id"]})

    def resolve_review(self, clinic_id: str, actor_id: str, reservation_id: str, action: str,
                       reason: str, expected_version: int) -> dict[str, Any]:
        action = choice(action, "复核处置", {"release", "deduct"})
        reason = text(reason, "复核理由", maximum=600)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:adjust", clinic_id=clinic_id)
            row = self._reservation(connection, clinic_id, reservation_id)
            if row is None:
                raise NotFound("权益占用记录不存在")
            require_match(row["version"], expected_version, "权益占用记录")
            if row["state"] != "pending_review":
                raise Conflict("该占用不在待复核状态")
            rest = row["sessions"] - row["consumed_sessions"]
            if rest <= 0:
                raise Conflict("该占用没有待复核次数")
            entry_type = "review_release" if action == "release" else "review_deduct"
            state = "review_released" if action == "release" else "review_deducted"
            self._entry(connection, grant_id=row["grant_id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                        item_code=row["item_code"], entry_type=entry_type, sessions=rest,
                        delta=rest if action == "release" else 0, actor_id=actor_id, reason=reason,
                        key=f"resolve:{reservation_id}", now=now,
                        appointment_id=row["appointment_id"], reservation_id=reservation_id)
            connection.execute("UPDATE entitlement_reservations SET state=?,updated_at=?,version=version+1 WHERE id=?",
                               (state, now, reservation_id))
            self._refresh_grant_state(connection, row["grant_id"])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_reservation", aggregate_id=reservation_id,
                               action="entitlement.review_resolved", occurred_at=now,
                               payload={"action": action, "sessions": rest, "reason": reason,
                                        "appointment_id": row["appointment_id"]})
        return {"id": reservation_id, "state": state, "sessions": rest, "version": expected_version + 1}

    def supplement(self, clinic_id: str, actor_id: str, grant_id: str, sessions: Any,
                   reason: str, idempotency_key: str) -> dict[str, Any]:
        sessions = integer(sessions, "补录次数", minimum=1, maximum=10000)
        reason = text(reason, "补录理由", maximum=600)
        key = require_idempotency_key(idempotency_key)
        now = self.now()
        request_hash = request_digest({"clinic_id": clinic_id, "grant_id": grant_id,
                                       "sessions": sessions, "reason": reason})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:adjust", clinic_id=clinic_id)
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_supplement' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("补录幂等编号已用于其他内容")
                return {**decode_json(previous["response_json"]), "replayed": True}
            grant = connection.execute("SELECT * FROM entitlement_grants WHERE id=? AND clinic_id=?",
                                       (grant_id, clinic_id)).fetchone()
            if grant is None:
                raise NotFound("权益发放记录不存在")
            if grant["state"] == "void":
                raise Conflict("已作废的发放记录不能补录")
            if grant["state"] == "expired":
                raise Conflict("已过期权益不能补录，请按新发放处理")
            entry_id = self._entry(connection, grant_id=grant_id, clinic_id=clinic_id, patient_id=grant["patient_id"],
                                   item_code=grant["item_code"], entry_type="supplement", sessions=sessions,
                                   delta=sessions, actor_id=actor_id, reason=reason, key=f"supplement:{key}", now=now)
            if grant["state"] == "exhausted":
                connection.execute("UPDATE entitlement_grants SET state='active',version=version+1 WHERE id=?", (grant_id,))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=grant["patient_id"],
                               aggregate_type="entitlement_grant", aggregate_id=grant_id, action="entitlement.supplemented",
                               occurred_at=now, payload={"sessions": sessions, "reason": reason, "entry_id": entry_id})
            result = {"grant_id": grant_id, "entry_id": entry_id, "sessions": sessions, "reason": reason,
                      "available": self._available(connection, grant_id)}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("entitlement_supplement", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    def reverse(self, clinic_id: str, actor_id: str, entry_id: str, reason: str, idempotency_key: str) -> dict[str, Any]:
        reason = text(reason, "冲正理由", maximum=600)
        key = require_idempotency_key(idempotency_key)
        now = self.now()
        request_hash = request_digest({"clinic_id": clinic_id, "entry_id": entry_id, "reason": reason})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:adjust", clinic_id=clinic_id)
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_reverse' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("冲正幂等编号已用于其他内容")
                return {**decode_json(previous["response_json"]), "replayed": True}
            entry = connection.execute("SELECT * FROM entitlement_entries WHERE id=? AND clinic_id=?",
                                       (entry_id, clinic_id)).fetchone()
            if entry is None:
                raise NotFound("台账分录不存在")
            if entry["entry_type"] not in REVERSIBLE_TYPES:
                raise Conflict("该类型分录不能冲正", details={"entry_type": entry["entry_type"]})
            if connection.execute("SELECT 1 FROM entitlement_entries WHERE reverses=?", (entry_id,)).fetchone():
                raise Conflict("该分录已被冲正，不能重复冲正")
            delta = -entry["sessions"] if entry["entry_type"] in {"grant", "supplement"} else entry["sessions"]
            if delta < 0:
                available = self._available(connection, entry["grant_id"])
                if available + delta < 0:
                    raise Conflict("额度已被使用，需先冲正相关核销分录", details={"available": available})
            settled = entry["sequence"] <= self._settled_watermarks(connection, clinic_id).get(entry["grant_id"], 0)
            new_entry_id = self._entry(
                connection, grant_id=entry["grant_id"], clinic_id=clinic_id, patient_id=entry["patient_id"],
                item_code=entry["item_code"], entry_type="reverse", sessions=entry["sessions"], delta=delta,
                actor_id=actor_id, reason=reason, key=f"reverse:{entry_id}", now=now,
                appointment_id=entry["appointment_id"], reservation_id=entry["reservation_id"],
                encounter_id=entry["encounter_id"], reverses=entry_id)
            self._refresh_grant_state(connection, entry["grant_id"])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=entry["patient_id"],
                               aggregate_type="entitlement_grant", aggregate_id=entry["grant_id"],
                               action="entitlement.reversed", occurred_at=now,
                               payload={"entry_id": entry_id, "entry_type": entry["entry_type"],
                                        "sessions": entry["sessions"], "delta": delta, "reason": reason,
                                        "reversed_settled_entry": settled, "new_entry_id": new_entry_id})
            result = {"entry_id": new_entry_id, "reversed_entry_id": entry_id, "grant_id": entry["grant_id"],
                      "delta": delta, "reversed_settled_entry": settled}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("entitlement_reverse", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    def expire_grants(self, clinic_id: str, *, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValidationError("处理数量必须为 1 至 1000")
        now = self.now()
        with self.db.transaction() as connection:
            today = self.local_date(connection, clinic_id)
            rows = connection.execute(
                "SELECT g.*,COALESCE(SUM(e.delta),0) AS available FROM entitlement_grants g "
                "LEFT JOIN entitlement_entries e ON e.grant_id=g.id "
                "WHERE g.clinic_id=? AND g.valid_until IS NOT NULL AND g.valid_until<? AND g.state IN ('active','expired') "
                "GROUP BY g.id ORDER BY g.valid_until,g.id", (clinic_id, today)).fetchall()
            processed = 0
            for row in rows:
                if processed >= limit:
                    break
                available = max(0, int(row["available"]))
                if row["state"] == "expired" and available == 0:
                    continue
                if available > 0:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM entitlement_entries WHERE grant_id=? AND entry_type='expire'",
                        (row["id"],)).fetchone()[0]
                    self._entry(connection, grant_id=row["id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                                item_code=row["item_code"], entry_type="expire", sessions=available, delta=-available,
                                actor_id=None, reason="权益超过有效期，剩余次数核销",
                                key=f"expire:{row['id']}:{count + 1}", now=now)
                if row["state"] != "expired":
                    connection.execute("UPDATE entitlement_grants SET state='expired',version=version+1 WHERE id=?", (row["id"],))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=None, patient_id=row["patient_id"],
                                   aggregate_type="entitlement_grant", aggregate_id=row["id"], action="entitlement.expired",
                                   occurred_at=now, payload={"item_code": row["item_code"], "sessions": available,
                                                             "valid_until": row["valid_until"]})
                processed += 1
        return {"expired": processed, "as_of": now}

    def release_for_appointment(self, connection, clinic_id: str, appointment_id: str,
                                reason: str, now: str, actor_id: str | None) -> int:
        """预约取消或占位过期时，在同一事务内返还全部待履约占用。"""
        rows = connection.execute(
            "SELECT r.*,g.item_code FROM entitlement_reservations r JOIN entitlement_grants g ON g.id=r.grant_id "
            "WHERE r.appointment_id=? AND r.clinic_id=? AND r.state='reserved'", (appointment_id, clinic_id)).fetchall()
        for row in rows:
            self._release_row(connection, row, reason, now, actor_id)
        return len(rows)

    def review_for_appointment(self, connection, clinic_id: str, appointment_id: str,
                               reason: str, now: str, actor_id: str | None) -> int:
        """患者未到诊时，在同一事务内将占用转为待财务复核。"""
        rows = connection.execute(
            "SELECT r.*,g.item_code FROM entitlement_reservations r JOIN entitlement_grants g ON g.id=r.grant_id "
            "WHERE r.appointment_id=? AND r.clinic_id=? AND r.state='reserved'", (appointment_id, clinic_id)).fetchall()
        for row in rows:
            self._entry(connection, grant_id=row["grant_id"], clinic_id=clinic_id, patient_id=row["patient_id"],
                        item_code=row["item_code"], entry_type="review_pending", sessions=row["sessions"], delta=0,
                        actor_id=actor_id, reason=reason, key=f"review-pending:{row['id']}", now=now,
                        appointment_id=appointment_id, reservation_id=row["id"])
            connection.execute(
                "UPDATE entitlement_reservations SET state='pending_review',updated_at=?,version=version+1 WHERE id=?",
                (now, row["id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_reservation", aggregate_id=row["id"],
                               action="entitlement.review_pending", occurred_at=now,
                               payload={"sessions": row["sessions"], "reason": reason,
                                        "appointment_id": appointment_id})
        return len(rows)

    def balance(self, clinic_id: str, actor_id: str, patient_id: str, *, item_code: str | None = None) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?",
                                  (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            clauses = ["patient_id=?", "clinic_id=?"]
            params: list[Any] = [patient_id, clinic_id]
            if item_code is not None:
                clauses.append("item_code=?")
                params.append(text(item_code, "项目编码", maximum=80))
            rows = connection.execute(
                f"SELECT * FROM entitlement_grants WHERE {' AND '.join(clauses)} ORDER BY item_code,created_at,id",
                params).fetchall()
            items: dict[str, dict[str, Any]] = {}
            for row in rows:
                breakdown = self._breakdown(connection, row["id"])
                bucket = items.setdefault(row["item_code"], {
                    "item_code": row["item_code"],
                    "totals": {"granted": 0, "consumed": 0, "held": 0, "pending_review": 0,
                               "expired": 0, "reversed_out": 0, "returned": 0, "available": 0},
                    "grants": []})
                for name, value in breakdown.items():
                    bucket["totals"][name] += value
                bucket["grants"].append({"id": row["id"], "source_type": row["source_type"],
                                         "source_ref": row["source_ref"], "total_sessions": row["total_sessions"],
                                         "rule_version": row["rule_version"], "valid_from": row["valid_from"],
                                         "valid_until": row["valid_until"], "state": row["state"],
                                         "scope": decode_json(row["scope_json"]), "created_at": row["created_at"],
                                         **breakdown})
            return {"patient_id": patient_id, "as_of": self.now(), "items": list(items.values())}

    def ledger(self, clinic_id: str, actor_id: str, patient_id: str, *, item_code: str | None = None,
               grant_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValidationError("查询数量须为 1 至 500")
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?",
                                  (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            clauses = ["e.patient_id=?", "e.clinic_id=?"]
            params: list[Any] = [patient_id, clinic_id]
            if item_code is not None:
                clauses.append("e.item_code=?")
                params.append(text(item_code, "项目编码", maximum=80))
            if grant_id is not None:
                clauses.append("e.grant_id=?")
                params.append(grant_id)
            params.append(limit)
            rows = connection.execute(
                "SELECT e.*,g.source_type,g.source_ref,g.rule_version FROM entitlement_entries e "
                "JOIN entitlement_grants g ON g.id=e.grant_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY e.created_at,e.id LIMIT ?", params).fetchall()
            return [{"id": row["id"], "sequence": row["sequence"], "grant_id": row["grant_id"],
                     "item_code": row["item_code"], "entry_type": row["entry_type"], "sessions": row["sessions"],
                     "delta": row["delta"], "source_type": row["source_type"], "source_ref": row["source_ref"],
                     "rule_version": row["rule_version"], "appointment_id": row["appointment_id"],
                     "reservation_id": row["reservation_id"], "encounter_id": row["encounter_id"],
                     "reverses": row["reverses"], "actor_id": row["actor_id"], "reason": row["reason"],
                     "created_at": row["created_at"]} for row in rows]

    def pending_reviews(self, clinic_id: str, actor_id: str) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT r.*,g.item_code,g.source_ref,a.kind AS appointment_kind,a.starts_at "
                "FROM entitlement_reservations r JOIN entitlement_grants g ON g.id=r.grant_id "
                "JOIN appointments a ON a.id=r.appointment_id "
                "WHERE r.clinic_id=? AND r.state='pending_review' ORDER BY r.updated_at,r.id", (clinic_id,)).fetchall()
            return [{"id": row["id"], "patient_id": row["patient_id"], "item_code": row["item_code"],
                     "source_ref": row["source_ref"], "appointment_id": row["appointment_id"],
                     "appointment_kind": row["appointment_kind"], "starts_at": row["starts_at"],
                     "sessions": row["sessions"], "consumed_sessions": row["consumed_sessions"],
                     "review_sessions": row["sessions"] - row["consumed_sessions"],
                     "updated_at": row["updated_at"], "version": row["version"]} for row in rows]

    def close_settlement(self, clinic_id: str, actor_id: str, period_start: str, period_end: str,
                         note: str, idempotency_key: str) -> dict[str, Any]:
        start = calendar_date(period_start, "结账起始日期")
        end = calendar_date(period_end, "结账结束日期")
        if end < start:
            raise ValidationError("结账结束日期不能早于起始日期")
        note = text(note, "结账说明", maximum=600)
        key = require_idempotency_key(idempotency_key)
        now = self.now()
        request_hash = request_digest({"clinic_id": clinic_id, "period_start": start,
                                       "period_end": end, "note": note})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:adjust", clinic_id=clinic_id)
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_settlement' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("结账幂等编号已用于其他内容")
                return {**decode_json(previous["response_json"]), "replayed": True}
            overlap = connection.execute(
                "SELECT id FROM entitlement_settlements WHERE clinic_id=? AND period_start<=? AND period_end>=?",
                (clinic_id, end, start)).fetchone()
            if overlap:
                raise Conflict("结账期间与已有结账记录重叠", details={"settlement_id": overlap["id"]})
            pending = connection.execute(
                "SELECT COUNT(*) FROM entitlement_reservations WHERE clinic_id=? AND state='pending_review'",
                (clinic_id,)).fetchone()[0]
            if pending:
                raise Conflict("存在待财务复核的次数，结账前必须完成处置", details={"pending_review": pending})
            grants = connection.execute(
                "SELECT * FROM entitlement_grants WHERE clinic_id=? ORDER BY item_code,patient_id,id",
                (clinic_id,)).fetchall()
            snapshot = []
            totals: dict[str, dict[str, int]] = {}
            for grant in grants:
                breakdown = self._breakdown(connection, grant["id"])
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM entitlement_entries WHERE grant_id=?",
                    (grant["id"],)).fetchone()[0]
                snapshot.append({"grant_id": grant["id"], "patient_id": grant["patient_id"],
                                 "item_code": grant["item_code"], "state": grant["state"],
                                 "rule_version": grant["rule_version"], "entry_sequence": sequence, **breakdown})
                bucket = totals.setdefault(grant["item_code"], {"granted": 0, "consumed": 0, "held": 0,
                                                                "pending_review": 0, "expired": 0,
                                                                "reversed_out": 0, "returned": 0, "available": 0})
                for name, value in breakdown.items():
                    bucket[name] += value
            settlement_id = new_id("est")
            connection.execute(
                "INSERT INTO entitlement_settlements(id,clinic_id,period_start,period_end,note,snapshot_json,closed_by,closed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (settlement_id, clinic_id, start, end, note, encode_json({"grants": snapshot, "totals": totals}),
                 actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="entitlement_settlement", aggregate_id=settlement_id,
                               action="entitlement.settlement_closed", occurred_at=now,
                               payload={"period_start": start, "period_end": end, "note": note,
                                        "grants": len(snapshot)})
            result = {"id": settlement_id, "period_start": start, "period_end": end, "note": note,
                      "grant_count": len(snapshot), "totals": totals, "closed_at": now}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("entitlement_settlement", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    def settlements(self, clinic_id: str, actor_id: str) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:read", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT id,period_start,period_end,note,closed_by,closed_at FROM entitlement_settlements "
                "WHERE clinic_id=? ORDER BY period_end,id", (clinic_id,)).fetchall()
            return [dict(row) for row in rows]

    def settlement_detail(self, clinic_id: str, actor_id: str, settlement_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "billing:read", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM entitlement_settlements WHERE id=? AND clinic_id=?",
                                     (settlement_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("结账记录不存在")
            snapshot = decode_json(row["snapshot_json"])
            return {"id": row["id"], "period_start": row["period_start"], "period_end": row["period_end"],
                    "note": row["note"], "closed_by": row["closed_by"], "closed_at": row["closed_at"],
                    "grants": snapshot["grants"], "totals": snapshot["totals"]}
