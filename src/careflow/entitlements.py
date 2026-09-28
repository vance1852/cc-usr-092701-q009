"""患者次数权益台账：发放、预约预留、履约核销与财务复核。

口径约定（余额永远由不可变流水求和，禁止改写任何台账行）：

- issue 发放 +n；hold 预约确认占用 -n；release 取消/结算释放 +n；
  redeem 服务签署后核销，增量 0（占用转已用）；deduct 财务复核后确认扣减，增量 0；
  review 待财务复核，增量 0（保持锁定，既不可用也不入已用）；
  review 行被 resolve 后追加 release(+n) 或 deduct(0)，并以 resolves_review 指回；
  return 冲正返还 +n；reverse 冲正扣回 -n；expire 期满作废 -n。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import calendar_date, choice, integer, object_value, parsed_timestamp, request_digest, text, timestamp

GRANT_SOURCES = {"purchase", "gift", "compensation", "transfer_in", "backfill"}
PRIVILEGED_SOURCES = {"compensation", "transfer_in"}
SETTLE_ACTIONS = ("redeem", "release", "review")


class EntitlementService:
    """按患者与服务项目管理次数额度。公开方法即业务边界。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    # ----- 服务项目目录 -------------------------------------------------

    def register_service(self, clinic_id: str, actor_id: str, code: str, name: str,
                         category: str, *, rule_version: str = "rules-v1") -> dict[str, Any]:
        code = text(code, "项目编号", maximum=40)
        name = text(name, "项目名称", maximum=160)
        category = text(category, "项目分类", maximum=80)
        rule_version = text(rule_version, "规则版本", maximum=40)
        service_id = new_id("svc")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "staff:manage", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM service_catalog WHERE clinic_id=? AND code=?", (clinic_id, code)).fetchone():
                raise Conflict("项目编号在本诊所已存在")
            connection.execute(
                "INSERT INTO service_catalog(id,clinic_id,code,name,category,active,rule_version,created_at) "
                "VALUES(?,?,?,?,?,1,?,?)", (service_id, clinic_id, code, name, category, rule_version, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="service_catalog", aggregate_id=service_id,
                               action="entitlement.cataloged", occurred_at=now,
                               payload={"code": code, "name": name, "rule_version": rule_version})
        return {"id": service_id, "code": code, "name": name, "category": category,
                "rule_version": rule_version, "active": True}

    def list_services(self, clinic_id: str, actor_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            sql = "SELECT * FROM service_catalog WHERE clinic_id=?"
            if not include_inactive:
                sql += " AND active=1"
            rows = connection.execute(sql + " ORDER BY code", (clinic_id,)).fetchall()
            return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def _require_service_codes(self, connection, clinic_id: str, codes: Any) -> list[str]:
        if not isinstance(codes, list) or not codes or len(codes) > 20:
            raise ValidationError("适用项目必须为 1 至 20 个项目编号")
        normalized: list[str] = []
        for code in codes:
            code = text(code, "适用项目编号", maximum=40)
            if connection.execute("SELECT 1 FROM service_catalog WHERE clinic_id=? AND code=? AND active=1",
                                  (clinic_id, code)).fetchone() is None:
                raise NotFound(f"项目不存在或已停用：{code}")
            normalized.append(code)
        if len(set(normalized)) != len(normalized):
            raise ValidationError("适用项目不能重复")
        return normalized

    # ----- 发放 ---------------------------------------------------------

    def grant(self, clinic_id: str, actor_id: str, patient_id: str, source: str, total_count: int,
              service_codes: list[str], starts_on: str, expires_at: str, reason: str,
              idempotency_key: str, *, source_ref: str | None = None,
              rule_version: str = "rules-v1") -> dict[str, Any]:
        source = choice(source, "额度来源", GRANT_SOURCES)
        key = require_idempotency_key(idempotency_key)
        count = integer(total_count, "发放次数", minimum=1, maximum=10000)
        start = calendar_date(starts_on, "生效日期")
        expires = timestamp(expires_at, "到期时间")
        reason = text(reason, "发放理由", maximum=600)
        source_ref = text(source_ref or "", "来源单号", minimum=0, maximum=120) or None
        rule_version = text(rule_version, "规则版本", maximum=40)
        if parsed_timestamp(expires) <= self.clock.now():
            raise ValidationError("到期时间必须晚于当前时间")
        grant_id = new_id("grt")
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "entitlement:write", clinic_id=clinic_id)
            if source in PRIVILEGED_SOURCES and principal.role != "owner":
                raise Forbidden("补偿与转入额度只能由诊所负责人登记")
            if source == "backfill":
                raise Forbidden("历史补录必须通过补录申请经财务复核后入账")
            patient = connection.execute("SELECT id,state FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能登记新额度")
            existing = connection.execute("SELECT id FROM entitlement_grants WHERE clinic_id=? AND idempotency_key=?",
                                          (clinic_id, key)).fetchone()
            if existing:
                return self.grant_detail(clinic_id, actor_id, existing["id"], replayed=True)
            codes = self._require_service_codes(connection, clinic_id, list(service_codes))
            scope = {"service_codes": codes}
            connection.execute(
                "INSERT INTO entitlement_grants(id,clinic_id,patient_id,source,source_ref,total_count,scope_json,"
                "starts_on,expires_at,rule_version,state,granted_by,reason,idempotency_key,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
                (grant_id, clinic_id, patient_id, source, source_ref, count, encode_json(scope),
                 start, expires, rule_version, actor_id, reason, key, now))
            self._append_ledger(connection, clinic_id=clinic_id, patient_id=patient_id, grant_id=grant_id,
                                entry_type="issue", delta=count, quantity=count,
                                reason=f"{source}:{reason}", actor_id=actor_id,
                                key=f"{grant_id}:issue", now=now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="entitlement_grant", aggregate_id=grant_id,
                               action="entitlement.granted", occurred_at=now,
                               payload={"source": source, "source_ref": source_ref, "count": count,
                                        "scope": scope, "expires_at": expires, "rule_version": rule_version})
        return self.grant_detail(clinic_id, actor_id, grant_id, replayed=False)

    def revoke_grant(self, clinic_id: str, actor_id: str, grant_id: str, reason: str) -> dict[str, Any]:
        """停用发放批次：仅冻结尚未预留的可用次数，已预留次数随预约结算。"""
        reason = text(reason, "停用原因", maximum=600)
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            if principal.role != "owner":
                raise Forbidden("只有诊所负责人可以停用发放批次")
            row = connection.execute("SELECT * FROM entitlement_grants WHERE id=? AND clinic_id=?", (grant_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("权益批次不存在")
            if row["state"] != "active":
                raise Conflict("只有生效中的批次可以停用")
            available = self._grant_available(connection, grant_id)
            connection.execute("UPDATE entitlement_grants SET state='revoked',expired_at=?,version=version+1 WHERE id=?",
                               (now, grant_id))
            if available > 0:
                self._append_ledger(connection, clinic_id=clinic_id, patient_id=row["patient_id"], grant_id=grant_id,
                                    entry_type="expire", delta=-available, quantity=available,
                                    reason=f"批次停用：{reason}", actor_id=actor_id,
                                    key=f"{grant_id}:revoke", now=now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_grant", aggregate_id=grant_id,
                               action="entitlement.revoked", occurred_at=now,
                               payload={"reason": reason, "available_revoked": available})
        return self.grant_detail(clinic_id, actor_id, grant_id)

    def expire_entitlements(self, clinic_id: str, actor_id: str | None = None, *, limit: int = 500) -> dict[str, Any]:
        """将已过有效期的批次可用次数结转为期满；已预留或待复核次数不受影响。"""
        if not 1 <= limit <= 5000:
            raise ValidationError("处理数量必须为 1 至 5000")
        now = self.now()
        with self.db.transaction() as connection:
            if actor_id is not None:
                authorize(principal_for(connection, actor_id, clinic_id), "entitlement:review", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT * FROM entitlement_grants WHERE clinic_id=? AND state='active' AND expires_at<=? "
                "ORDER BY expires_at,id LIMIT ?", (clinic_id, now, limit)).fetchall()
            expired = []
            for row in rows:
                available = self._grant_available(connection, row["id"])
                connection.execute("UPDATE entitlement_grants SET state='expired',expired_at=?,version=version+1 WHERE id=?",
                                   (now, row["id"]))
                if available > 0:
                    self._append_ledger(connection, clinic_id=clinic_id, patient_id=row["patient_id"], grant_id=row["id"],
                                        entry_type="expire", delta=-available, quantity=available,
                                        reason="有效期届满", actor_id=actor_id,
                                        key=f"{row['id']}:expire", now=now)
                expired.append({"grant_id": row["id"], "expired_count": available})
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                                   aggregate_type="entitlement_grant", aggregate_id=row["id"],
                                   action="entitlement.expired", occurred_at=now,
                                   payload={"expires_at": row["expires_at"], "expired_count": available})
        return {"as_of": now, "expired": expired}

    # ----- 预约生命周期挂钩（由预约状态机在同一事务内调用） -------------

    def hold_for_booking(self, connection, clinic_id: str, appointment, actor_id: str, now: str) -> list[dict[str, Any]]:
        """确认预约时按先到期先出跨批次占用；过期、停用或未生效批次不参与。

        余额不足时整笔抛出，由调用方回滚预约确认。
        """
        service_code = appointment["service_code"]
        if not service_code:
            return []
        quantity = appointment["service_quantity"] or 1
        existing = connection.execute(
            "SELECT 1 FROM entitlement_holds WHERE appointment_id=? AND state!='reversed'",
            (appointment["id"],)).fetchone()
        if existing:
            raise Conflict("该预约已存在权益预留，不能重复占用")
        tz_row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        from zoneinfo import ZoneInfo
        local_today = self.clock.now().astimezone(ZoneInfo(tz_row["timezone"])).date().isoformat()
        grants = connection.execute(
            "SELECT g.* FROM entitlement_grants g WHERE g.clinic_id=? AND g.patient_id=? AND g.state='active' "
            "AND g.starts_on<=? AND g.expires_at>? AND EXISTS ("
            "SELECT 1 FROM json_each(g.scope_json,'$.service_codes') WHERE value=?) "
            "ORDER BY g.expires_at,g.created_at,g.id",
            (clinic_id, appointment["patient_id"], local_today, now, service_code)).fetchall()
        plan: list[tuple[Any, int]] = []
        remaining = quantity
        for grant in grants:
            available = self._grant_available(connection, grant["id"])
            if available <= 0:
                continue
            take = min(available, remaining)
            plan.append((grant, take))
            remaining -= take
            if remaining == 0:
                break
        if remaining > 0:
            raise Conflict("患者该项目的可用次数不足，无法确认预约",
                           details={"service_code": service_code, "requested": quantity,
                                    "available": quantity - remaining})
        held = []
        for grant, take in plan:
            hold_id = new_id("hold")
            connection.execute(
                "INSERT INTO entitlement_holds(id,clinic_id,patient_id,grant_id,appointment_id,service_code,quantity,"
                "state,held_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'held',?,?,?)",
                (hold_id, clinic_id, appointment["patient_id"], grant["id"], appointment["id"],
                 service_code, take, actor_id, now, now))
            self._append_ledger(connection, clinic_id=clinic_id, patient_id=appointment["patient_id"],
                                grant_id=grant["id"], entry_type="hold", delta=-take, quantity=take,
                                hold_id=hold_id, appointment_id=appointment["id"],
                                reason=f"预约确认占用 {service_code}", actor_id=actor_id,
                                key=f"{hold_id}:hold", now=now)
            held.append({"hold_id": hold_id, "grant_id": grant["id"], "service_code": service_code,
                         "quantity": take, "grant_expires_at": grant["expires_at"]})
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=appointment["patient_id"],
                           aggregate_type="appointment", aggregate_id=appointment["id"],
                           action="entitlement.held", occurred_at=now,
                           payload={"service_code": service_code, "quantity": quantity, "holds": held})
        return held

    def release_on_cancel(self, connection, clinic_id: str, appointment, actor_id: str | None,
                          now: str, reason: str) -> int:
        """预约取消：仍处占用状态的预留全部释放回可用余额；待复核部分保持锁定。"""
        return self._dispose_all(connection, clinic_id, appointment, actor_id, now, reason, to_review=False)

    def refer_on_no_show(self, connection, clinic_id: str, appointment, actor_id: str | None,
                         now: str, reason: str) -> int:
        """未到诊：是否扣次不能由前台直接决定，占用转待财务复核。"""
        return self._dispose_all(connection, clinic_id, appointment, actor_id, now, reason, to_review=True)

    def _dispose_all(self, connection, clinic_id: str, appointment, actor_id: str | None, now: str,
                     reason: str, *, to_review: bool) -> int:
        holds = connection.execute(
            "SELECT * FROM entitlement_holds WHERE appointment_id=? AND clinic_id=? AND state IN ('held','partial_redeemed') "
            "ORDER BY id", (appointment["id"], clinic_id)).fetchall()
        for hold in holds:
            remaining = self._hold_open_quantity(connection, hold["id"])
            if remaining <= 0:
                continue
            if to_review:
                self._append_hold_entry(connection, clinic_id, hold, "review", remaining, now, actor_id, reason)
                connection.execute("UPDATE entitlement_holds SET state='in_review',updated_at=?,version=version+1 WHERE id=?",
                                   (now, hold["id"]))
            else:
                self._append_hold_entry(connection, clinic_id, hold, "release", remaining, now, actor_id, reason)
                target = "partial_redeemed" if self._hold_used(connection, hold["id"]) > 0 else "released"
                connection.execute("UPDATE entitlement_holds SET state=?,updated_at=?,version=version+1 WHERE id=?",
                                   (target, now, hold["id"]))
        action = "entitlement.no_show_referred" if to_review else "entitlement.cancel_released"
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=appointment["patient_id"],
                           aggregate_type="appointment", aggregate_id=appointment["id"], action=action,
                           occurred_at=now, payload={"reason": reason, "holds": len(holds)})
        return len(holds)

    # ----- 履约结算 -----------------------------------------------------

    def settle_appointment(self, clinic_id: str, actor_id: str, appointment_id: str,
                           lines: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        """按实际完成内容逐项目结算：核销、释放、待财务复核必须显式分列。

        核销必须存在已签署就诊记录；同一预约可多次结算，但累计数量不能超过预留。
        """
        reason = text(reason, "结算说明", maximum=600)
        line_keys = {"redeem": "redeemed", "release": "released", "review": "review"}
        normalized = []
        for line in lines or []:
            object_value(line, "结算行", allowed={"service_code", "redeemed", "released", "review"})
            code = text(line.get("service_code", ""), "项目编号", maximum=40)
            amounts = {action: integer(line.get(line_keys[action], 0), f"{code} {line_keys[action]} 数量",
                                       minimum=0, maximum=10000)
                       for action in SETTLE_ACTIONS}
            if sum(amounts.values()) <= 0:
                raise ValidationError(f"{code} 结算数量不能全部为零")
            normalized.append((code, amounts))
        if not normalized:
            raise ValidationError("至少提供一条项目结算行")
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "appointment:write", clinic_id=clinic_id)
            appointment = connection.execute("SELECT * FROM appointments WHERE id=? AND clinic_id=?",
                                             (appointment_id, clinic_id)).fetchone()
            if appointment is None:
                raise NotFound("预约不存在")
            if appointment["state"] not in {"in_service", "completed", "cancelled", "no_show"}:
                raise Conflict("只有开始服务或已结束的预约可以结算权益")
            encounter_id = None
            if any(amounts["redeem"] > 0 for _, amounts in normalized):
                if principal.role not in {"clinician", "nurse", "owner"}:
                    raise Forbidden("实际核销必须由临床岗位依据已签署记录执行")
                encounter = connection.execute(
                    "SELECT id FROM encounters WHERE appointment_id=? AND state='signed'", (appointment_id,)).fetchone()
                if encounter is None:
                    raise Conflict("存在核销数量时，必须已有已签署的就诊记录作为实际完成依据")
                encounter_id = encounter["id"]
            result_lines = []
            for code, amounts in normalized:
                holds = connection.execute(
                    "SELECT h.* FROM entitlement_holds h JOIN entitlement_grants g ON g.id=h.grant_id "
                    "WHERE h.appointment_id=? AND h.clinic_id=? AND h.service_code=? "
                    "AND h.state IN ('held','partial_redeemed') ORDER BY g.expires_at,g.created_at,h.id",
                    (appointment_id, clinic_id, code)).fetchall()
                open_total = sum(self._hold_open_quantity(connection, hold["id"]) for hold in holds)
                requested = sum(amounts.values())
                if requested > open_total:
                    raise Conflict("结算数量不能超过该项目尚未处置的预留数量",
                                   details={"service_code": code, "requested": requested, "open": open_total})
                pools = dict(amounts)
                entries = []
                for hold in holds:
                    remaining = self._hold_open_quantity(connection, hold["id"])
                    allocations = []
                    for action in SETTLE_ACTIONS:
                        take = min(remaining, pools[action])
                        if take > 0:
                            self._append_hold_entry(connection, clinic_id, hold, action, take, now, actor_id,
                                                    reason, encounter_id=encounter_id)
                            allocations.append((action, take))
                            pools[action] -= take
                            remaining -= take
                    if allocations:
                        entries.append({"hold_id": hold["id"], "grant_id": hold["grant_id"],
                                        "allocations": [{"action": action, "quantity": take} for action, take in allocations]})
                        self._refresh_hold_state(connection, hold["id"], now)
                result_lines.append({"service_code": code, "redeemed": amounts["redeem"],
                                     "released": amounts["release"], "review": amounts["review"],
                                     "holds": entries})
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=appointment["patient_id"],
                               aggregate_type="appointment", aggregate_id=appointment_id,
                               action="entitlement.settled", occurred_at=now,
                               payload={"lines": result_lines, "reason": reason})
        return {"appointment_id": appointment_id, "lines": result_lines, "reason": reason}

    def resolve_review(self, clinic_id: str, actor_id: str, ledger_sequence: int,
                       decision: str, reason: str) -> dict[str, Any]:
        """财务对单笔待复核数量作终局决定：释放返还或确认扣减，每笔只能决定一次。"""
        decision = choice(decision, "复核决定", {"release", "deduct"})
        reason = text(reason, "复核理由", maximum=600)
        sequence = integer(ledger_sequence, "台账流水号", minimum=1)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:review", clinic_id=clinic_id)
            review = connection.execute("SELECT * FROM entitlement_ledger WHERE sequence=? AND entry_type='review'",
                                        (sequence,)).fetchone()
            if review is None or review["clinic_id"] != clinic_id:
                raise NotFound("待复核台账流水不存在")
            resolved = connection.execute(
                "SELECT 1 FROM entitlement_ledger WHERE resolves_review=?", (review["id"],)).fetchone()
            if resolved:
                raise Conflict("该笔待复核数量已经作出决定")
            hold = connection.execute("SELECT * FROM entitlement_holds WHERE id=?", (review["hold_id"],)).fetchone()
            grant = connection.execute("SELECT state FROM entitlement_grants WHERE id=?", (review["grant_id"],)).fetchone()
            if decision == "release":
                if grant["state"] != "active":
                    raise Conflict("原批次已停用或期满，不能直接返还；请改走补录流程")
                entry_type, delta = "release", review["quantity"]
            else:
                entry_type, delta = "deduct", 0
            self._append_ledger(connection, clinic_id=clinic_id, patient_id=review["patient_id"],
                                grant_id=review["grant_id"], entry_type=entry_type, delta=delta,
                                quantity=review["quantity"], hold_id=review["hold_id"],
                                appointment_id=review["appointment_id"], resolves_review=review["id"],
                                reason=f"财务复核{('返还' if decision == 'release' else '扣减')}：{reason}",
                                actor_id=actor_id, key=f"{review['id']}:resolve", now=now)
            self._refresh_hold_state(connection, hold["id"], now, bump=True)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=review["patient_id"],
                               aggregate_type="entitlement_hold", aggregate_id=hold["id"],
                               action=f"entitlement.review_{decision}", occurred_at=now,
                               payload={"review_sequence": sequence, "quantity": review["quantity"], "reason": reason})
            final_state = connection.execute("SELECT state FROM entitlement_holds WHERE id=?", (hold["id"],)).fetchone()[0]
        return {"review_sequence": sequence, "hold_id": hold["id"], "decision": decision,
                "quantity": review["quantity"], "hold_state": final_state, "resolved_at": now}

    # ----- 补录与冲正（申请/审批分离） ---------------------------------

    def request_backfill(self, clinic_id: str, actor_id: str, patient_id: str, quantity: int,
                         service_codes: list[str], expires_at: str, reason: str, idempotency_key: str,
                         *, source_ref: str | None = None, evidence_ref: str | None = None) -> dict[str, Any]:
        expires = timestamp(expires_at, "补录到期时间")
        if parsed_timestamp(expires) <= self.clock.now():
            raise ValidationError("补录额度的到期时间必须晚于当前时间")
        return self._request_adjustment(
            clinic_id, actor_id, kind="backfill", patient_id=patient_id, quantity=quantity, reason=reason,
            idempotency_key=idempotency_key, target_ledger=None,
            detail={"service_codes": list(service_codes), "expires_at": expires,
                    "source_ref": text(source_ref or "", "来源单号", minimum=0, maximum=120) or None},
            evidence_ref=evidence_ref, validate_detail=lambda conn: self._require_service_codes(
                conn, clinic_id, list(service_codes)))

    def request_reversal(self, clinic_id: str, actor_id: str, patient_id: str, ledger_sequence: int,
                         quantity: int, reason: str, idempotency_key: str, *,
                         evidence_ref: str | None = None) -> dict[str, Any]:
        sequence = integer(ledger_sequence, "台账流水号", minimum=1)
        return self._request_adjustment(
            clinic_id, actor_id, kind="reversal", patient_id=patient_id, quantity=quantity, reason=reason,
            idempotency_key=idempotency_key, target_ledger=sequence, detail=None, evidence_ref=evidence_ref)

    def _request_adjustment(self, clinic_id: str, actor_id: str, *, kind: str, patient_id: str,
                            quantity: int, reason: str, idempotency_key: str,
                            target_ledger: int | None, detail: dict[str, Any] | None,
                            evidence_ref: str | None, validate_detail=None) -> dict[str, Any]:
        patient_id = require_id(patient_id, "患者编号")
        count = integer(quantity, "数量", minimum=1, maximum=10000)
        reason = text(reason, "理由", minimum=10, maximum=1000)
        evidence = text(evidence_ref or "", "凭据编号", minimum=0, maximum=160) or None
        key = require_idempotency_key(idempotency_key)
        now = self.now()
        adjustment_id = new_id("adj")
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "entitlement:write", clinic_id=clinic_id)
            patient = connection.execute("SELECT id FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            request_hash = request_digest({"kind": kind, "patient_id": patient_id, "quantity": count,
                                           "target": target_ledger, "detail": detail, "reason": reason})
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='entitlement_adjustment' AND key=?",
                (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("幂等编号已用于不同的补录或冲正请求")
                previous_id = decode_json(previous["response_json"])["id"]
                row = connection.execute("SELECT * FROM entitlement_adjustments WHERE id=?", (previous_id,)).fetchone()
                return self._adjustment_result(row, replayed=True)
            if kind == "backfill":
                codes = validate_detail(connection)
                detail = {**detail, "service_codes": codes}
            else:
                target = connection.execute(
                    "SELECT l.*,g.clinic_id AS grant_clinic FROM entitlement_ledger l "
                    "JOIN entitlement_grants g ON g.id=l.grant_id WHERE l.sequence=?", (target_ledger,)).fetchone()
                if target is None or target["grant_clinic"] != clinic_id or target["patient_id"] != patient_id:
                    raise NotFound("待冲正的台账流水不存在")
                if target["entry_type"] not in {"issue", "redeem", "deduct", "return", "reverse"}:
                    raise Conflict("该类流水不允许冲正；取消预约请使用释放，未到诊请走财务复核")
                already = connection.execute(
                    "SELECT COALESCE(SUM(l.quantity),0) FROM entitlement_ledger l "
                    "JOIN entitlement_adjustments a ON a.id=l.review_id "
                    "WHERE a.ledger_sequence=? AND a.state='approved' AND l.entry_type IN ('return','reverse')",
                    (target_ledger,)).fetchone()[0]
                impact = target["quantity"]
                if already + count > impact:
                    raise Conflict("冲正数量不能超过该流水尚未冲正的数量",
                                   details={"impact": impact, "already_reversed": int(already)})
            connection.execute(
                "INSERT INTO entitlement_adjustments(id,clinic_id,patient_id,kind,grant_id,ledger_sequence,quantity,"
                "reason,evidence_ref,detail_json,state,requested_by,idempotency_key,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (adjustment_id, clinic_id, patient_id, kind, None, target_ledger, count, reason, evidence,
                 encode_json(detail or {}), actor_id, key, now))
            connection.execute(
                "INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                ("entitlement_adjustment", key, request_hash, encode_json({"id": adjustment_id}), now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="entitlement_adjustment", aggregate_id=adjustment_id,
                               action=f"entitlement.{kind}_requested", occurred_at=now,
                               payload={"quantity": count, "target": target_ledger, "reason": reason,
                                        "evidence_ref": evidence})
            row = connection.execute("SELECT * FROM entitlement_adjustments WHERE id=?", (adjustment_id,)).fetchone()
        return self._adjustment_result(row, replayed=False)

    def review_adjustment(self, clinic_id: str, actor_id: str, adjustment_id: str, decision: str,
                          *, review_note: str | None = None) -> dict[str, Any]:
        decision = choice(decision, "审批决定", {"approved", "rejected"})
        review_note = text(review_note or "", "审批意见", minimum=0, maximum=1000) or None
        if decision == "rejected" and not review_note:
            raise ValidationError("驳回必须填写审批意见")
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "entitlement:review", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM entitlement_adjustments WHERE id=? AND clinic_id=?",
                                     (adjustment_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("补录或冲正申请不存在")
            if row["state"] != "pending":
                raise Conflict("该申请已完成审批")
            # 权限分离：申请人不能审批自己的单据。
            if row["requested_by"] == actor_id:
                raise Forbidden("申请与审批必须由不同人员完成")
            if decision == "approved":
                self._apply_adjustment(connection, clinic_id, row, actor_id, now)
            else:
                connection.execute(
                    "UPDATE entitlement_adjustments SET state='rejected',reviewed_by=?,review_note=?,reviewed_at=?,"
                    "version=version+1 WHERE id=?", (actor_id, review_note, now, adjustment_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_adjustment", aggregate_id=adjustment_id,
                               action=f"entitlement.adjustment_{decision}", occurred_at=now,
                               payload={"kind": row["kind"], "quantity": row["quantity"], "review_note": review_note})
            refreshed = connection.execute("SELECT * FROM entitlement_adjustments WHERE id=?", (adjustment_id,)).fetchone()
        return self._adjustment_result(refreshed, replayed=False)

    def _apply_adjustment(self, connection, clinic_id: str, row, reviewer_id: str, now: str) -> None:
        if row["kind"] == "backfill":
            detail = decode_json(row["detail_json"])
            grant_id = new_id("grt")
            codes = detail["service_codes"]
            connection.execute(
                "INSERT INTO entitlement_grants(id,clinic_id,patient_id,source,source_ref,total_count,scope_json,"
                "starts_on,expires_at,rule_version,state,granted_by,reason,idempotency_key,created_at) "
                "VALUES(?,?,?,'backfill',?,?,?,?,?,?,'active',?,?,?,?)",
                (grant_id, clinic_id, row["patient_id"], detail.get("source_ref"), row["quantity"],
                 encode_json({"service_codes": codes}), now[:10], detail["expires_at"], "rules-v1",
                 reviewer_id, f"补录批准：{row['reason']}", f"adjustment:{row['id']}", now))
            self._append_ledger(connection, clinic_id=clinic_id, patient_id=row["patient_id"], grant_id=grant_id,
                                entry_type="issue", delta=row["quantity"], quantity=row["quantity"],
                                review_id=row["id"], reason=f"历史补录：{row['reason']}",
                                actor_id=reviewer_id, key=f"{grant_id}:issue", now=now)
            connection.execute(
                "UPDATE entitlement_adjustments SET state='approved',reviewed_by=?,reviewed_at=?,grant_id=?,"
                "detail_json=?,version=version+1 WHERE id=?",
                (reviewer_id, now, grant_id, encode_json({**detail, "grant_id": grant_id}), row["id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=reviewer_id, patient_id=row["patient_id"],
                               aggregate_type="entitlement_grant", aggregate_id=grant_id,
                               action="entitlement.granted", occurred_at=now,
                               payload={"source": "backfill", "count": row["quantity"],
                                        "adjustment_id": row["id"], "scope": {"service_codes": codes}})
            return
        target = connection.execute("SELECT * FROM entitlement_ledger WHERE sequence=?", (row["ledger_sequence"],)).fetchone()
        grant = connection.execute("SELECT * FROM entitlement_grants WHERE id=?", (target["grant_id"],)).fetchone()
        if target["entry_type"] in {"redeem", "deduct"}:
            entry_type, delta = "return", row["quantity"]
            reason = f"冲正返还：{row['reason']}"
            if grant["state"] != "active":
                raise Conflict("原批次已停用或期满，不能直接返还；请改走补录流程")
        else:
            entry_type, delta = "reverse", -row["quantity"]
            reason = f"冲正扣回：{row['reason']}"
            if self._grant_available(connection, grant["id"]) < row["quantity"]:
                raise Conflict("可用次数不足，无法完成该冲正扣回")
        self._append_ledger(connection, clinic_id=clinic_id, patient_id=grant["patient_id"], grant_id=grant["id"],
                            entry_type=entry_type, delta=delta, quantity=row["quantity"],
                            review_id=row["id"], reversal_of=target["sequence"], reason=reason,
                            actor_id=reviewer_id, key=f"adjustment:{row['id']}:apply", now=now)
        connection.execute(
            "UPDATE entitlement_adjustments SET state='approved',reviewed_by=?,reviewed_at=?,grant_id=?,"
            "version=version+1 WHERE id=?", (reviewer_id, now, grant["id"], row["id"]))

    def list_adjustments(self, clinic_id: str, actor_id: str, *, state: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:review", clinic_id=clinic_id)
            sql = "SELECT * FROM entitlement_adjustments WHERE clinic_id=?"
            params: tuple[Any, ...] = (clinic_id,)
            if state is not None:
                state = choice(state, "审批状态", {"pending", "approved", "rejected"})
                sql += " AND state=?"
                params += (state,)
            rows = connection.execute(sql + " ORDER BY created_at,id", params).fetchall()
            return [self._adjustment_result(row) for row in rows]

    # ----- 查询：余额与逐笔溯源 -----------------------------------------

    def patient_balance(self, clinic_id: str, actor_id: str, patient_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            grants = connection.execute(
                "SELECT * FROM entitlement_grants WHERE patient_id=? AND clinic_id=? ORDER BY created_at,id",
                (patient_id, clinic_id)).fetchall()
            now = self.now()
            grant_views, by_code = [], {}
            for grant in grants:
                view = self._grant_view(connection, grant)
                grant_views.append(view)
                for code in decode_json(grant["scope_json"])["service_codes"]:
                    bucket = by_code.setdefault(code, {"issued": 0, "held": 0, "in_review": 0,
                                                       "used": 0, "available": 0})
                    for key in bucket:
                        bucket[key] += view[key]
            return {"patient_id": patient_id, "as_of": now,
                    "services": [{"service_code": code, **totals} for code, totals in sorted(by_code.items())],
                    "grants": grant_views}

    def patient_ledger(self, clinic_id: str, actor_id: str, patient_id: str,
                       *, service_code: str | None = None) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            rows = connection.execute(
                "SELECT * FROM entitlement_ledger WHERE patient_id=? AND clinic_id=? ORDER BY sequence",
                (patient_id, clinic_id)).fetchall()
            scopes = {row["id"]: decode_json(row["scope_json"])["service_codes"] for row in
                      connection.execute("SELECT id,scope_json FROM entitlement_grants WHERE patient_id=?",
                                         (patient_id,)).fetchall()}
            entries = [self._ledger_row(row) for row in rows]
            if service_code:
                entries = [entry for entry in entries if service_code in scopes.get(entry["grant_id"], [])]
            return {"patient_id": patient_id, "as_of": self.now(), "entries": entries}

    def grant_detail(self, clinic_id: str, actor_id: str, grant_id: str, *, replayed: bool = False) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            grant = connection.execute("SELECT * FROM entitlement_grants WHERE id=? AND clinic_id=?",
                                       (grant_id, clinic_id)).fetchone()
            if grant is None:
                raise NotFound("权益批次不存在")
            rows = connection.execute("SELECT * FROM entitlement_ledger WHERE grant_id=? ORDER BY sequence",
                                      (grant_id,)).fetchall()
            return {**self._grant_view(connection, grant),
                    "entries": [self._ledger_row(row) for row in rows], "replayed": replayed}

    def appointment_holds(self, clinic_id: str, actor_id: str, appointment_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "entitlement:read", clinic_id=clinic_id)
            appointment = connection.execute("SELECT id,patient_id FROM appointments WHERE id=? AND clinic_id=?",
                                             (appointment_id, clinic_id)).fetchone()
            if appointment is None:
                raise NotFound("预约不存在")
            rows = connection.execute(
                "SELECT h.*,g.source AS grant_source,g.expires_at AS grant_expires_at FROM entitlement_holds h "
                "JOIN entitlement_grants g ON g.id=h.grant_id WHERE h.appointment_id=? ORDER BY h.id",
                (appointment_id,)).fetchall()
            return {"appointment_id": appointment_id, "patient_id": appointment["patient_id"],
                    "holds": [{"id": row["id"], "grant_id": row["grant_id"], "service_code": row["service_code"],
                               "quantity": row["quantity"], "state": row["state"], "version": row["version"],
                               "grant_source": row["grant_source"], "grant_expires_at": row["grant_expires_at"],
                               "used": self._hold_used(connection, row["id"]),
                               "in_review": self._hold_in_review(connection, row["id"]),
                               "open": self._hold_open_quantity(connection, row["id"])} for row in rows]}

    # ----- 内部记账原语 -------------------------------------------------

    def _append_ledger(self, connection, *, clinic_id: str, patient_id: str, grant_id: str, entry_type: str,
                       delta: int, quantity: int, reason: str, actor_id: str | None, key: str, now: str,
                       hold_id: str | None = None, appointment_id: str | None = None,
                       encounter_id: str | None = None, review_id: str | None = None,
                       reversal_of: int | None = None, resolves_review: str | None = None) -> str:
        entry_id = new_id("led")
        connection.execute(
            "INSERT INTO entitlement_ledger(id,clinic_id,patient_id,grant_id,entry_type,count_delta,quantity,"
            "hold_id,appointment_id,encounter_id,review_id,reversal_of,resolves_review,reason,actor_id,"
            "idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, clinic_id, patient_id, grant_id, entry_type, delta, quantity, hold_id, appointment_id,
             encounter_id, review_id, reversal_of, resolves_review, reason, actor_id, key, now))
        return entry_id

    def _append_hold_entry(self, connection, clinic_id: str, hold, entry_type: str, quantity: int, now: str,
                           actor_id: str | None, reason: str, *, encounter_id: str | None = None) -> None:
        deltas = {"hold": -quantity, "release": quantity, "redeem": 0, "review": 0}
        n = connection.execute("SELECT COUNT(*) FROM entitlement_ledger WHERE hold_id=?", (hold["id"],)).fetchone()[0]
        self._append_ledger(connection, clinic_id=clinic_id, patient_id=hold["patient_id"], grant_id=hold["grant_id"],
                            entry_type=entry_type, delta=deltas[entry_type], quantity=quantity,
                            hold_id=hold["id"], appointment_id=hold["appointment_id"], encounter_id=encounter_id,
                            reason=reason, actor_id=actor_id, key=f"{hold['id']}:{entry_type}:{n + 1}", now=now)

    @staticmethod
    def _grant_available(connection, grant_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(count_delta),0) FROM entitlement_ledger WHERE grant_id=?", (grant_id,)).fetchone()
        return int(row[0])

    @staticmethod
    def _hold_used(connection, hold_id: str) -> int:
        """已最终核销/扣减的数量，包括待复核经财务确认扣减的部分。"""
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM entitlement_ledger "
            "WHERE hold_id=? AND entry_type IN ('redeem','deduct')", (hold_id,)).fetchone()
        return int(row[0])

    @staticmethod
    def _hold_in_review(connection, hold_id: str) -> int:
        """仍待财务决定的数量：review 行减去已有 resolve 行。"""
        row = connection.execute(
            "SELECT COALESCE((SELECT SUM(quantity) FROM entitlement_ledger WHERE hold_id=? AND entry_type='review'),0)"
            "-COALESCE((SELECT SUM(r.quantity) FROM entitlement_ledger r JOIN entitlement_ledger v ON v.id=r.resolves_review "
            "WHERE v.hold_id=?),0)", (hold_id, hold_id)).fetchone()
        return max(0, int(row[0]))

    def _hold_open_quantity(self, connection, hold_id: str) -> int:
        """仍占用、尚未作任何处置的数量（核销/释放/送审都会消耗）。"""
        row = connection.execute(
            "SELECT quantity FROM entitlement_holds WHERE id=?", (hold_id,)).fetchone()
        decided = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM entitlement_ledger WHERE hold_id=? "
            "AND entry_type IN ('redeem','release','review') AND resolves_review IS NULL", (hold_id,)).fetchone()[0]
        return int(row["quantity"]) - int(decided)

    def _refresh_hold_state(self, connection, hold_id: str, now: str, *, bump: bool = False) -> None:
        hold = connection.execute("SELECT quantity FROM entitlement_holds WHERE id=?", (hold_id,)).fetchone()
        used = self._hold_used(connection, hold_id)
        review = self._hold_in_review(connection, hold_id)
        open_q = self._hold_open_quantity(connection, hold_id)
        if review > 0:
            state = "in_review"
        elif open_q > 0 and used > 0:
            state = "partial_redeemed"
        elif open_q > 0:
            state = "held"
        elif used > 0:
            state = "redeemed"
        else:
            state = "released"
        suffix = ",version=version+1" if bump else ""
        connection.execute(f"UPDATE entitlement_holds SET state=?,updated_at=?{suffix} WHERE id=?",
                           (state, now, hold_id))

    def _grant_view(self, connection, grant) -> dict[str, Any]:
        def sum_q(where: str, *params) -> int:
            row = connection.execute(
                f"SELECT COALESCE(SUM(quantity),0) FROM entitlement_ledger WHERE grant_id=? {where}",
                (grant["id"], *params)).fetchone()
            return int(row[0])

        issued = int(grant["total_count"])
        # 已用 = 核销 + 复核扣减 - 冲正返还。
        used = (sum_q("AND entry_type IN ('redeem','deduct')") - sum_q("AND entry_type='return'"))
        used = max(0, used)
        in_review = self._pending_review_total(connection, grant["id"])
        # 占用中 = 累计占用 - 已直接释放/核销/送审（复核返还经由送审口径，不重复扣减）。
        held = max(0, sum_q("AND entry_type='hold'")
                   - sum_q("AND entry_type IN ('release','redeem','review') AND resolves_review IS NULL"))
        available = max(0, self._grant_available(connection, grant["id"]))
        return {"id": grant["id"], "patient_id": grant["patient_id"], "source": grant["source"],
                "source_ref": grant["source_ref"], "scope": decode_json(grant["scope_json"]),
                "starts_on": grant["starts_on"], "expires_at": grant["expires_at"],
                "rule_version": grant["rule_version"], "state": grant["state"],
                "granted_by": grant["granted_by"], "reason": grant["reason"], "created_at": grant["created_at"],
                "issued": issued, "held": held, "in_review": in_review, "used": used, "available": available}

    @staticmethod
    def _pending_review_total(connection, grant_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE((SELECT SUM(quantity) FROM entitlement_ledger WHERE grant_id=? AND entry_type='review'),0)"
            "-COALESCE((SELECT SUM(r.quantity) FROM entitlement_ledger r WHERE r.grant_id=? AND r.resolves_review IS NOT NULL),0)",
            (grant_id, grant_id)).fetchone()
        return max(0, int(row[0]))

    @staticmethod
    def _ledger_row(row) -> dict[str, Any]:
        return {"sequence": row["sequence"], "id": row["id"], "grant_id": row["grant_id"],
                "entry_type": row["entry_type"], "count_delta": row["count_delta"], "quantity": row["quantity"],
                "hold_id": row["hold_id"], "appointment_id": row["appointment_id"],
                "encounter_id": row["encounter_id"], "review_id": row["review_id"],
                "reversal_of": row["reversal_of"], "resolves_review": row["resolves_review"],
                "reason": row["reason"], "actor_id": row["actor_id"], "created_at": row["created_at"]}

    @staticmethod
    def _adjustment_result(row, *, replayed: bool = False) -> dict[str, Any]:
        return {"id": row["id"], "patient_id": row["patient_id"], "kind": row["kind"],
                "grant_id": row["grant_id"], "ledger_sequence": row["ledger_sequence"],
                "quantity": row["quantity"], "reason": row["reason"], "evidence_ref": row["evidence_ref"],
                "state": row["state"], "requested_by": row["requested_by"], "reviewed_by": row["reviewed_by"],
                "review_note": row["review_note"], "created_at": row["created_at"], "reviewed_at": row["reviewed_at"],
                "version": row["version"], "replayed": replayed}
