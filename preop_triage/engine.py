"""Policy engine: pure function from Facts to decision, issues, and checks.

Every number comes from the Policy. Every issue carries evidence pointing at a
source path or a grounded quote. Rules that cannot be evaluated fail closed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from .model import Assumption, Check, Computed, EngineResult, Evidence, Fact, Issue, ReviewFlag
from .policy import Policy

CATEGORY_FOR_RULE = {
    "R0": "MISSING_REQUIRED_DATA",
    "R1": "REQUIRED_DOCUMENTATION",
    "R1.1": "REQUIRED_DOCUMENTATION",
    "R1.2": "REQUIRED_DOCUMENTATION",
    "R2": "REQUIRED_TESTING",
    "R3": "ANTICOAGULATION_MANAGEMENT",
    "R4": "MISSING_REQUIRED_DATA",  # an unverifiable vital is missing data, never an exclusion
}


class _Ctx:
    def __init__(self, facts: list[Fact], policy: Policy) -> None:
        self.policy = policy
        self.by_kind: dict[str, list[Fact]] = defaultdict(list)
        for f in facts:
            self.by_kind[f.kind].append(f)
        self.docs: dict[str, Fact] = {f.doc_id: f for f in self.by_kind["DOC"] if f.doc_id}
        self.issues: list[Issue] = []
        self.checks: list[Check] = []
        self.assumptions: list[Assumption] = []
        self.flags: list[ReviewFlag] = []

    def one(self, kind: str):
        xs = self.by_kind.get(kind) or []
        return xs[0].value if xs else None

    def evidence(self, f: Fact) -> Evidence:
        d = self.docs.get(f.doc_id or "")
        return Evidence(
            source=f.source,
            doc_id=f.doc_id,
            date=(d.value.get("date") if d else (f.observed_at.isoformat() if f.observed_at else None)),
            author=(d.value.get("author") if d else None),
            quote=f.quote,
        )

    def ev_path(self, source: str, quote: str | None = None) -> Evidence:
        return Evidence(source=source, quote=quote)

    def issue(self, rule: str, status: str, summary: str, evidence: list[Evidence], action: str, computed: Computed | None = None, category: str | None = None) -> None:
        self.issues.append(Issue(category=category or CATEGORY_FOR_RULE[rule], rule_id=rule, status=status, summary=summary, evidence=evidence, computed=computed or Computed(), action_needed=action))

    def check(self, rule: str, status: str, summary: str, evidence: list[Evidence] | None = None, computed: Computed | None = None) -> None:
        self.checks.append(Check(rule_id=rule, status=status, summary=summary, evidence=evidence or [], computed=computed or Computed()))

    def assume(self, text: str, evidence: list[Evidence] | None = None) -> None:
        self.assumptions.append(Assumption(text=text, evidence=evidence or []))


def _days_before(pdate: date, when: datetime) -> int:
    return (pdate - when.date()).days


def _latest(facts: Iterable[Fact]) -> Fact | None:
    xs = [f for f in facts if f.observed_at is not None]
    return max(xs, key=lambda f: f.observed_at) if xs else None  # type: ignore[arg-type,return-value]


# --- R0 / vitals helpers -------------------------------------------------------


def _review_time(ctx: _Ctx) -> datetime | None:
    rt = ctx.one("REVIEW_TIME")
    if rt is not None:
        return rt
    stamps = [f.observed_at for fs in ctx.by_kind.values() for f in fs if f.observed_at is not None]
    if not stamps:
        return None
    fallback = max(stamps)
    ctx.assume(f"Review time missing; using latest timestamp in package ({fallback.isoformat()}) as review time.")
    return fallback


def _latest_vital(ctx: _Ctx, kind: str, review_time: datetime | None) -> tuple[Fact | None, bool]:
    """Return (latest fact, verifiable). Structured wins ties; later readings after review time are ignored."""
    facts = ctx.by_kind.get(kind) or []
    if any(f.observed_at is None for f in facts):
        for f in facts:
            if f.observed_at is None:
                ctx.flags.append(ReviewFlag("VITAL_TIMESTAMP_UNKNOWN", f.doc_id, f.quote, None, None, "R4"))
        return None, False
    usable = []
    for f in facts:
        if review_time is not None and f.observed_at > review_time:
            ctx.assume(f"{kind} reading at {f.observed_at.isoformat()} is after review time and was ignored.", [ctx.evidence(f)])
            continue
        usable.append(f)
    if not usable:
        return None, True
    best_time = max(f.observed_at for f in usable)
    tied = [f for f in usable if f.observed_at == best_time]
    structured = [f for f in tied if f.parser == "structured"]
    if len(tied) > 1 and structured:
        ctx.assume(f"{kind}: structured reading and free-text reading share the same timestamp; tie resolved in favor of the structured value.", [ctx.evidence(f) for f in tied])
        return structured[0], True
    if len(tied) > 1:
        # same document date and time: the reading that appears later in the text is the later one
        tied.sort(key=lambda f: (f.doc_id or "", f.meta.get("offset") if f.meta.get("offset") is not None else -1), reverse=True)
        if len({f.value for f in tied}) > 1:
            ctx.assume(f"{kind}: several free-text readings share the same timestamp; the one appearing last in the document was used.", [ctx.evidence(f) for f in tied])
    return tied[0], True


# --- Rules ---------------------------------------------------------------------


def _r0_and_r4(ctx: _Ctx, pdate: date | None, risk: str | None) -> None:
    p = ctx.policy
    r0_fail = False
    if pdate is None:
        r0_fail = True
        ev = [ctx.ev_path("procedure.procedure_date")] + [ctx.evidence(f) for f in ctx.by_kind.get("PROCEDURE_DATE_STATEMENT", []) if f.value.get("label") == "PENDING"]
        ctx.issue("R0", "PROCEDURE_DATE_MISSING", "Procedure date is not set, so no timing window can be evaluated.", ev, "Schedule the procedure date and resubmit.")
    if risk is None:
        r0_fail = True
        ev = [ctx.ev_path("procedure.procedure_risk")] + [ctx.evidence(f) for f in ctx.by_kind.get("PROCEDURE_RISK_STATEMENT", [])]
        ctx.issue("R0", "PROCEDURE_RISK_MISSING", "Procedure risk level is not assigned, so required testing cannot be determined.", ev, "Surgical team to assign LOW/MODERATE/HIGH risk.")
    elif risk not in p.required_tests_by_risk:
        r0_fail = True
        raw = next((f.meta.get("raw") for f in ctx.by_kind.get("PROCEDURE_RISK", []) if f.meta.get("raw")), None)
        ev = [ctx.ev_path("procedure.procedure_risk", raw)]
        ctx.issue("R0", "PROCEDURE_RISK_UNKNOWN", f"Procedure risk level '{risk}' is not one of {', '.join(p.required_tests_by_risk)}, so required testing cannot be determined.", ev, "Surgical team to assign LOW/MODERATE/HIGH risk.")

    review_time = _review_time(ctx)
    bp, bp_ok = _latest_vital(ctx, "BP", review_time)
    temp, temp_ok = _latest_vital(ctx, "TEMP", review_time)
    deferred = [ctx.evidence(f) for f in ctx.by_kind.get("VITALS_DEFERRED", [])]

    r4_evidence: list[Evidence] = []
    r4_fail = False
    if bp_ok and bp is None:
        r0_fail = True
        ctx.issue("R0", "BP_MISSING", "No blood pressure reading found in structured vitals or documents.", [ctx.ev_path("vitals")] + deferred, "Obtain and record a blood pressure.")
    elif bp is not None:
        s, d = bp.value
        comp = Computed(observed_date=bp.observed_at.isoformat(), observed_value=f"{s}/{d} mmHg", threshold=f"systolic >= {p.thresholds.sbp} or diastolic >= {p.thresholds.dbp}")
        r4_evidence.append(ctx.evidence(bp))
        if s >= p.thresholds.sbp or d >= p.thresholds.dbp:
            r4_fail = True
            which = f"systolic >= {p.thresholds.sbp}" if s >= p.thresholds.sbp else f"diastolic >= {p.thresholds.dbp}"
            if s >= p.thresholds.sbp and d >= p.thresholds.dbp:
                which = f"systolic >= {p.thresholds.sbp} and diastolic >= {p.thresholds.dbp}"
            ctx.issue("R4", "BP_EXCLUSION", f"Most recent blood pressure {s}/{d} mmHg on {bp.observed_at.date().isoformat()} meets the exclusion threshold ({which}).", [ctx.evidence(bp)], "Hold scheduling; recheck blood pressure and document a within-range reading.", Computed(observed_date=comp.observed_date, observed_value=comp.observed_value, threshold=which), "ACUTE_SAFETY_EXCLUSION")
    if temp_ok and temp is None:
        r0_fail = True
        ctx.issue("R0", "TEMP_MISSING", "No temperature reading found in structured vitals or documents.", [ctx.ev_path("vitals")] + deferred, "Obtain and record a temperature.")
    elif temp is not None:
        t: Decimal = temp.value
        if temp.meta.get("unit_inferred"):
            ctx.assume(f"Temperature reading {temp.quote!r} has no unit; read as {temp.meta['unit_inferred']} from its value range.", [ctx.evidence(temp)])
        comp = Computed(observed_date=temp.observed_at.isoformat(), observed_value=f"{t} F", threshold=f"temperature > {p.thresholds.temp_f} F", converted_from=temp.meta.get("converted_from"))
        r4_evidence.append(ctx.evidence(temp))
        if t > p.thresholds.temp_f:
            r4_fail = True
            ctx.issue("R4", "TEMP_EXCLUSION", f"Most recent temperature {t} F on {temp.observed_at.date().isoformat()} exceeds {p.thresholds.temp_f} F.", [ctx.evidence(temp)], "Hold scheduling; evaluate for infection and recheck temperature.", comp, "ACUTE_SAFETY_EXCLUSION")

    ctx.check("R0", "FAIL" if r0_fail else "PASS", "Required fields present" if not r0_fail else "Required data missing")
    if not (bp_ok and temp_ok):
        ctx.check("R4", "UNVERIFIED", "A vital sign could not be verified", r4_evidence)
    else:
        ctx.check("R4", "FAIL" if r4_fail else "PASS", "Acute safety exclusion present" if r4_fail else "Most recent vitals within limits", r4_evidence)


def _r1_hp(ctx: _Ctx, pdate: date | None) -> None:
    p = ctx.policy
    kinds = ctx.by_kind.get("DOC_KIND", [])
    preop = [f for f in kinds if f.value == "PREOP_HP"]
    nonq = [f for f in kinds if f.value == "NON_QUALIFYING_HP"]
    absence = [ctx.evidence(f) for f in ctx.by_kind.get("DOC_ABSENCE", []) if f.value == "HP_ABSENT"]
    if not preop:
        if nonq:
            latest = _latest(nonq) or nonq[0]
            ctx.flags.append(ReviewFlag("ONLY_NON_QUALIFYING_HP", latest.doc_id, None, None, None, "R1.1"))
            ctx.issue("R1.1", "NON_QUALIFYING", "The only H&P-titled documents are wellness or establish-care visits, not a pre-operative H&P.", [ctx.evidence(f) for f in nonq] + absence, "Obtain a pre-operative H&P within 30 days of the procedure.")
        else:
            ctx.issue("R1.1", "MISSING", "No pre-operative History and Physical found in the package.", absence or [ctx.ev_path("documents")], "Obtain a pre-operative H&P within 30 days of the procedure.")
        ctx.check("R1.1", "FAIL", "Pre-op H&P missing")
        return
    latest = _latest(preop)
    if latest is None:
        ctx.flags.append(ReviewFlag("HP_DATE_UNKNOWN", preop[0].doc_id, None, None, None, "R1.1"))
        ctx.check("R1.1", "UNVERIFIED", "Pre-op H&P has no usable date", [ctx.evidence(f) for f in preop])
        return
    ev = [ctx.evidence(latest)]
    if pdate is None:
        ctx.check("R1.1", "SKIPPED", "Pre-op H&P present; window not evaluable without a procedure date", ev)
        return
    days = _days_before(pdate, latest.observed_at)
    comp = Computed(procedure_date=pdate.isoformat(), observed_date=latest.observed_at.date().isoformat(), days_before=days, window_days=p.windows.hp_days)
    if 0 <= days <= p.windows.hp_days:
        ctx.check("R1.1", "PASS", f"Pre-op H&P dated {days} days before the procedure (window {p.windows.hp_days})", ev, comp)
    else:
        ctx.issue("R1.1", "OUTDATED", f"Most recent pre-op H&P is {days} days before the procedure; the limit is {p.windows.hp_days} days.", ev, "Obtain an updated pre-operative H&P.", comp)
        ctx.check("R1.1", "FAIL", "Pre-op H&P outside window", ev, comp)


def _r1_consent(ctx: _Ctx, ptype: str | None) -> None:
    from .classify import procedure_matches

    consents = [f for f in ctx.by_kind.get("DOC_KIND", []) if f.value == "CONSENT"]
    absence = [ctx.evidence(f) for f in ctx.by_kind.get("DOC_ABSENCE", []) if f.value == "CONSENT_ABSENT"]
    if not consents:
        ctx.issue("R1.2", "MISSING", "No surgical consent document found in the package.", absence or [ctx.ev_path("documents")], "Obtain a signed surgical consent for the scheduled procedure.")
        ctx.check("R1.2", "FAIL", "Consent missing")
        return
    sigs = defaultdict(list)
    procs = defaultdict(list)
    for f in ctx.by_kind.get("CONSENT_SIG", []):
        sigs[f.doc_id].append(f)
    for f in ctx.by_kind.get("CONSENT_PROC", []):
        procs[f.doc_id].append(f)

    signed_matching: list[Fact] = []
    signed_unverifiable: list[Fact] = []
    wrong: list[tuple[Fact, Fact]] = []
    unsigned: list[Fact] = []
    unverifiable_sig: list[Fact] = []
    for c in sorted(consents, key=lambda f: (f.observed_at or datetime.min), reverse=True):
        labels = {s.value for s in sigs.get(c.doc_id, [])}
        if labels == {"SIGNED"}:
            lines = procs.get(c.doc_id, [])
            match = procedure_matches(ptype, lines[0].value, ctx.policy) if lines else None
            if match is True:
                signed_matching.append(c)
            elif match is False:
                wrong.append((c, lines[0]))
            else:
                signed_unverifiable.append(c)
        elif labels == {"UNSIGNED"}:
            unsigned.append(c)
        else:  # no signature statement found, or contradictory statements
            unverifiable_sig.append(c)

    if signed_matching:
        best = signed_matching[0]
        ev = [ctx.evidence(s) for s in sigs[best.doc_id]] + [ctx.evidence(l) for l in procs[best.doc_id]]
        ctx.check("R1.2", "PASS", "Signed consent for the scheduled procedure on file", ev)
        return
    if wrong and not signed_unverifiable and not unverifiable_sig:
        c, line = wrong[0]
        ev = [ctx.evidence(line), ctx.ev_path("procedure.procedure_type", ptype)] + [ctx.evidence(s) for s in sigs[c.doc_id]]
        ctx.issue("R1.2", "WRONG_PROCEDURE", f"The only signed consent is for '{line.value}', not the scheduled procedure '{ptype}'.", ev, "Obtain a signed consent naming the scheduled procedure.")
        ctx.check("R1.2", "FAIL", "Consent names a different procedure", ev)
        return
    if signed_unverifiable or unverifiable_sig:
        c = (signed_unverifiable or unverifiable_sig)[0]
        reason = "CONSENT_PROCEDURE_UNVERIFIED" if signed_unverifiable else "CONSENT_SIGNATURE_UNVERIFIED"
        ctx.flags.append(ReviewFlag(reason, c.doc_id, None, None, None, "R1.2"))
        ev = [ctx.evidence(c)] + [ctx.evidence(s) for s in sigs.get(c.doc_id, [])] + [ctx.evidence(l) for l in procs.get(c.doc_id, [])]
        ctx.issue("R1.2", "UNVERIFIED", "A consent is on file but its signature or procedure could not be verified from the text.", ev, "Nurse to confirm the consent is patient-signed and names the scheduled procedure.")
        ctx.check("R1.2", "UNVERIFIED", "Consent could not be verified", ev)
        return
    latest = unsigned[0]
    ev = [ctx.evidence(s) for s in sigs[latest.doc_id]] + absence
    ctx.issue("R1.2", "UNSIGNED", "Consent exists but has not been signed by the patient.", ev, "Obtain the patient's signature on the surgical consent.")
    ctx.check("R1.2", "FAIL", "Consent unsigned", ev)


def _r2_testing(ctx: _Ctx, pdate: date | None, risk: str | None) -> None:
    p = ctx.policy
    if risk is None or risk not in p.required_tests_by_risk:
        ctx.check("R2", "SKIPPED", "Procedure risk unknown; required tests not evaluable")
        return
    window = p.windows.lab_days[risk]
    fail = False
    ev_all: list[Evidence] = []
    for code in p.required_tests_by_risk[risk]:
        labs = [f for f in ctx.by_kind.get("LAB", []) if f.value["code"] == code]
        excluded = [f for f in labs if f.value["status"] not in p.lab_final_statuses]
        final = [f for f in labs if f.value["status"] in p.lab_final_statuses]
        latest = _latest(final)
        excluded_ev = [ctx.evidence(f) for f in excluded]
        if latest is None:
            fail = True
            ctx.issue("R2", "MISSING", f"No final {code} result on file (required for {risk} risk).", [ctx.ev_path("labs")] + excluded_ev, f"Obtain a {code} within {window} days of the procedure.", Computed(window_days=window))
            continue
        ev = [ctx.evidence(latest)] + excluded_ev
        ev_all += ev
        if pdate is None:
            continue
        days = _days_before(pdate, latest.observed_at)
        comp = Computed(procedure_date=pdate.isoformat(), observed_date=latest.observed_at.date().isoformat(), days_before=days, window_days=window)
        if not (0 <= days <= window):
            fail = True
            ctx.issue("R2", "OUTDATED", f"Most recent final {code} is {days} days before the procedure; the {risk}-risk limit is {window} days.", ev, f"Repeat the {code}.", comp)
    if fail:
        ctx.check("R2", "FAIL", "Required testing missing or outdated", ev_all)
    elif pdate is None:
        ctx.check("R2", "SKIPPED", "Required tests present; windows not evaluable without a procedure date", ev_all)
    else:
        ctx.check("R2", "PASS", f"Required tests for {risk} risk within {window} days", ev_all)


_ANTICOAG_CONTEXT = re.compile(r"anticoag|interrupt|periprocedural|perioperative|\bhold|resum|restart|bridg", re.IGNORECASE)


def _latest_mentions(mentions: list[Fact]) -> list[Fact]:
    """The most recent note wins; undated mentions only count when nothing is dated."""
    dated = [m for m in mentions if m.observed_at is not None]
    if not dated:
        return mentions
    newest = max(m.observed_at for m in dated)  # type: ignore[type-var]
    return [m for m in dated if m.observed_at == newest]


def _r3_anticoag(ctx: _Ctx) -> None:
    meds = [f for f in ctx.by_kind.get("MED", []) if f.value["class"] == "anticoagulant"]
    mentions = [f for f in ctx.by_kind.get("MED_MENTION", []) if f.value["class"] in ("anticoagulant",) and f.value["canonical"]]
    active: dict[str, list[Evidence]] = {}
    unknown: dict[str, list[Evidence]] = {}
    for m in meds:
        canon = m.value["canonical"] or m.value["name"]
        ev = [ctx.ev_path(m.source, m.value["name"])]
        status = m.value["active"]
        related = [x for x in mentions if x.value["canonical"] == canon]
        latest = _latest_mentions(related)
        labels = {x.value["label"] for x in latest}
        if status is True:
            active.setdefault(canon, []).extend(ev)
        elif status is False:
            if "TAKING" in labels:
                taking = next(x for x in latest if x.value["label"] == "TAKING")
                ctx.flags.append(ReviewFlag("MED_STATUS_CONFLICT", taking.doc_id, taking.quote, None, None, "R3"))
            elif len(related) > len(latest):
                ctx.assume(f"{canon}: older notes mention it, but the medication list and the most recent note agree it is not being taken.", [ctx.evidence(x) for x in latest])
        else:
            if labels == {"TAKING"}:
                ctx.assume(f"{canon}: medication list status blank; treated as active because the most recent note says the patient is taking it.", [ctx.evidence(x) for x in latest])
                active.setdefault(canon, []).extend(ev + [ctx.evidence(x) for x in latest])
            elif labels == {"NOT_TAKING"}:
                ctx.assume(f"{canon}: medication list status blank; treated as inactive because the most recent note says it was stopped.", [ctx.evidence(x) for x in latest])
            else:
                unknown[canon] = ev + [ctx.evidence(x) for x in related]
    listed = {(m.value["canonical"] or m.value["name"]) for m in meds}
    for canon in sorted({x.value["canonical"] for x in mentions} - listed):
        latest = _latest_mentions([x for x in mentions if x.value["canonical"] == canon])
        labels = {x.value["label"] for x in latest}
        if labels == {"TAKING"}:
            ctx.assume(f"{canon}: absent from the medication list but patient-reported as taken in a note; treated as active.", [ctx.evidence(x) for x in latest])
            active.setdefault(canon, []).extend(ctx.evidence(x) for x in latest)
        elif "UNKNOWN" in labels or len(labels) > 1:
            unknown.setdefault(canon, []).extend(ctx.evidence(x) for x in latest)

    for canon, ev in unknown.items():
        ctx.issue("R0", "ANTICOAG_STATUS_UNKNOWN", f"Cannot determine whether the patient is currently taking {canon}.", ev, f"Confirm whether the patient is taking {canon}; if so, obtain a perioperative plan.")
    if unknown and not active:
        ctx.check("R3", "SKIPPED", "Anticoagulant status unknown; evaluated as missing data")
        return
    if not active:
        ctx.check("R3", "SKIPPED", "No active anticoagulant")
        return

    holds = ctx.by_kind.get("ANTICOAG_HOLD", [])
    resumes = ctx.by_kind.get("ANTICOAG_RESUME", [])
    deferred = [f for f in ctx.by_kind.get("ANTICOAG_DEFERRED", []) if f.value["canonical"] or _ANTICOAG_CONTEXT.search(f.quote or "")]
    fail = False
    pass_ev: list[Evidence] = []
    def _for(facts_: list[Fact], canon: str) -> list[Fact]:
        return [f for f in facts_ if f.value["label"] == "SPECIFIC" and (f.value["canonical"] == canon or (f.value["canonical"] is None and f.value.get("doc_canonical") == canon))]

    for canon, ev in active.items():
        h = _for(holds, canon)
        r = _for(resumes, canon)
        pair = next(((hh, rr) for hh in h for rr in r if hh.doc_id == rr.doc_id), None)
        if pair:
            for part in pair:
                if part["value"]["canonical"] is None if isinstance(part, dict) else part.value["canonical"] is None:
                    ctx.assume(f"Plan line {part.quote!r} names no drug; attributed to {canon}, the only anticoagulant that document discusses.", [ctx.evidence(part)])
            pass_ev += [ctx.evidence(pair[0]), ctx.evidence(pair[1])]
            continue
        fail = True
        other_plan = [f for f in holds + resumes if f.value["canonical"] not in (None, canon) and f.value["label"] == "SPECIFIC"]
        any_mention = [f for f in holds + resumes + deferred if f.value["canonical"] in (None, canon)]
        plan_ev = [ctx.evidence(f) for f in any_mention]
        if h or r:
            missing = "restart" if h else "hold"
            ctx.issue("R3", "INCOMPLETE_PLAN", f"The perioperative plan for {canon} does not specify when to {missing} the medication.", ev + plan_ev, f"Document the {missing} timing for {canon}.")
        elif any_mention:
            ctx.issue("R3", "INCOMPLETE_PLAN", f"The only anticoagulation note for {canon} defers the decision or is vague; no hold and restart timing is documented.", ev + plan_ev, f"Obtain a perioperative plan for {canon} with hold and restart timing.")
        elif other_plan:
            ctx.issue("R3", "STALE_PLAN", f"The documented plan covers {other_plan[0].value['canonical']}, not the {canon} the patient now takes.", ev + [ctx.evidence(f) for f in other_plan], f"Obtain a perioperative plan for {canon}.")
        else:
            ctx.issue("R3", "NO_PLAN", f"Patient takes {canon} and no perioperative anticoagulation plan is documented.", ev, f"Obtain a perioperative plan for {canon} with hold and restart timing.")
    ctx.check("R3", "FAIL" if fail else "PASS", "Anticoagulation plan incomplete" if fail else "Anticoagulation plan documents hold and restart", pass_ev)


def _apply_flags(ctx: _Ctx, external: list[ReviewFlag]) -> None:
    all_flags = external + ctx.flags
    ctx.flags = all_flags
    seen = set()
    for fl in all_flags:
        if not fl.blocking:
            continue
        rule = fl.rule_id or "R0"
        base = rule.split(".")[0]
        category = CATEGORY_FOR_RULE.get(rule, CATEGORY_FOR_RULE.get(base, "MISSING_REQUIRED_DATA"))
        if fl.reason in ("ONLY_NON_QUALIFYING_HP", "CONSENT_PROCEDURE_UNVERIFIED", "CONSENT_SIGNATURE_UNVERIFIED"):
            continue  # these rules already emitted their own issue
        key = (category, fl.reason, fl.doc_id, fl.quote)
        if key in seen:
            continue
        seen.add(key)
        ev = [Evidence(source=f"documents:{fl.doc_id}" if fl.doc_id else "review_flag", doc_id=fl.doc_id, quote=fl.quote)]
        ctx.issue(rule if rule in CATEGORY_FOR_RULE else "R0", "UNVERIFIED", f"Input to {rule} could not be verified ({fl.reason}).", ev, "Nurse to review the flagged text.", category=category)


def evaluate(facts: list[Fact], review_flags: list[ReviewFlag], policy: Policy) -> EngineResult:
    ctx = _Ctx(facts, policy)
    pdate: date | None = ctx.one("PROCEDURE_DATE")
    risk: str | None = ctx.one("PROCEDURE_RISK")
    ptype: str | None = ctx.one("PROCEDURE_TYPE")

    _r0_and_r4(ctx, pdate, risk)
    _r1_hp(ctx, pdate)
    _r1_consent(ctx, ptype)
    _r2_testing(ctx, pdate, risk)
    _r3_anticoag(ctx)
    _apply_flags(ctx, review_flags)

    ctx.checks.sort(key=lambda c: c.rule_id)
    ctx.issues.sort(key=lambda i: (i.category, i.rule_id, i.status, i.summary))
    if any(i.category == "ACUTE_SAFETY_EXCLUSION" for i in ctx.issues):
        decision = "NOT_CLEARED"
    elif ctx.issues or any(f.blocking for f in ctx.flags):
        decision = "NEEDS_FOLLOW_UP"
    else:
        decision = "READY"
    return EngineResult(decision=decision, issues=ctx.issues, checks=ctx.checks, assumptions=ctx.assumptions, review_flags=ctx.flags)
