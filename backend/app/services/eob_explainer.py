"""Deterministic, plain-language explanation of a bill + insurer EOB (claim).  W8.

* No network, no AI: the same inputs always give the same output.  All math uses ``Decimal``.
* Aimed at a 6th-8th grade reading level (short sentences, plain words; terms are defined in ``glossary``).
* Every number and every flag quotes the exact field(s) it came from (``source`` / ``fields``), so the patient
  can check it against the paper bill or EOB.
* It EXPLAINS the numbers the patient entered.  It is an estimate/explanation, NOT legal or financial advice,
  and not a decision by the insurer or provider.

Optional LLM hook (OFF by default):  set env ``EOB_LLM_EXPLAINER=1`` AND call ``set_llm_explainer(fn)`` where
``fn(result: dict) -> str``.  The text is returned separately as ``llm_note`` and labelled as AI-written; the
deterministic result is never changed by it.  If the hook is off or fails, nothing is sent anywhere.

Inputs may be ORM objects or dicts; see ``explain_bill_eob``.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable

log = logging.getLogger("health-portal.eob")

TOL = Decimal("0.01")
ZERO = Decimal("0.00")
DISCLAIMER = ("This is an estimate and a plain-language explanation of the numbers you entered. "
              "It is not legal or financial advice, and it is not a decision by your insurer or provider. "
              "Check it against your paper bill and EOB, and ask the billing office or insurer if anything is unclear.")

GLOSSARY = {
    "EOB": "An Explanation of Benefits is a letter from your insurer. It is not a bill. It shows how a claim was handled.",
    "Billed amount": "The full price the provider listed before any insurance discount.",
    "Allowed amount": "The most your plan will count for this service. Providers in your plan's network agree to it.",
    "Plan discount": "The part of the price the provider agreed not to charge. It is billed amount minus allowed amount.",
    "Insurer paid": "What your insurance company paid the provider.",
    "Deductible": "What you pay first each year before your plan starts to share the cost.",
    "Copay": "A flat fee you pay for a visit or service.",
    "Coinsurance": "Your percent share of the allowed amount after the deductible.",
    "Patient responsibility": "The total your insurer says you owe: deductible plus copay plus coinsurance.",
}

STATUS_WORDS = {
    "submitted": "was sent to your insurer",
    "processing": "is still being processed",
    "paid": "was processed and paid",
    "partially_denied": "was only partly covered",
    "denied": "was denied",
    "appealed": "is being appealed",
}


# --------------------------------------------------------------------------- helpers

def D(x: Any) -> Decimal | None:
    """Money -> Decimal with 2 places (None / '' stay None).  Floats are converted through str, never trusted."""
    if x is None or x == "":
        return None
    if isinstance(x, Decimal):
        d = x
    else:
        try:
            d = Decimal(str(x).replace("$", "").replace(",", "").strip())
        except Exception:
            return None
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(d: Decimal | None) -> str:
    return "not given" if d is None else f"${d:,.2f}"


def _get(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def _near(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOL


def _src(field: str, value: Any) -> dict:
    if isinstance(value, Decimal):
        value = f"{value:.2f}"
    elif isinstance(value, date):
        value = value.isoformat()
    return {"field": field, "value": None if value is None else str(value)}


def _flag(code: str, severity: str, title: str, message: str, fields: list[dict], ask: str | None = None) -> dict:
    return {"code": code, "severity": severity, "title": title, "message": message, "fields": fields, "ask": ask}


def readability_grade(text: str) -> float:
    """Flesch-Kincaid grade estimate (rough; used by tests to keep the wording simple)."""
    sentences = [s for s in re.split(r"[.!?]+(?:\s|$)", text) if s.strip()]
    words = re.findall(r"[A-Za-z']+", text)
    if not sentences or not words:
        return 0.0

    def syll(w: str) -> int:
        w = w.lower()
        groups = re.findall(r"[aeiouy]+", w)
        n = len(groups)
        if w.endswith("e") and n > 1 and not w.endswith("le"):
            n -= 1
        return max(1, n)

    s = sum(syll(w) for w in words)
    return 0.39 * (len(words) / len(sentences)) + 11.8 * (s / len(words)) - 15.59


# optional LLM hook (OFF by default)
_LLM: Callable[[dict], str] | None = None


def set_llm_explainer(fn: Callable[[dict], str] | None) -> None:
    global _LLM
    _LLM = fn


def llm_enabled() -> bool:
    return os.environ.get("EOB_LLM_EXPLAINER", "0").strip().lower() in ("1", "true", "yes", "on") and _LLM is not None


# --------------------------------------------------------------------------- main entry

def explain_bill_eob(bill: Any, claim: Any = None, plan: Any = None, *,
                     payments_total: Any = None, related_bills: Iterable[Any] = (),
                     related_claims: Iterable[Any] = ()) -> dict:
    """Explain one bill (and its matched claim/EOB, if any).

    bill:  provider_name, service_date, billed_amount, amount_due, due_date, account_number, id
    claim: claim_number, status, billed_amount, allowed_amount, insurer_paid, deductible, copay, coinsurance,
           patient_responsibility, denial_reason, service_date, id
    plan:  insurer_name, effective_date, end_date, deductible_annual (all optional)
    payments_total: what the patient already paid toward this bill
    related_bills / related_claims: OTHER rows of the same patient, used to spot duplicate service dates.
    """
    flags: list[dict] = []
    missing: list[dict] = []
    lines: list[dict] = []

    b_billed, b_due = D(_get(bill, "billed_amount")), D(_get(bill, "amount_due"))
    paid = D(payments_total) or ZERO
    balance = (b_due - paid) if b_due is not None else None
    provider = _get(bill, "provider_name") or "the provider"
    svc_date = _get(bill, "service_date")

    out: dict[str, Any] = {
        "estimate_label": "Estimate / explanation only - not legal or financial advice",
        "disclaimer": DISCLAIMER, "glossary": GLOSSARY, "has_claim": claim is not None,
        "claim_status": _get(claim, "status") if claim is not None else None,
        "bill_balance": None if balance is None else f"{balance:.2f}",
    }

    def line(key, label, amount, plain, source):
        lines.append({"key": key, "label": label, "amount": None if amount is None else f"{amount:.2f}",
                      "display": money(amount), "plain": plain, "source": source})

    # ---------- the bill itself ----------
    line("bill_billed", "Price on the bill", b_billed, "The full price the provider listed.",
         _src("bill.billed_amount", b_billed))
    line("bill_due", "Provider is asking you to pay", b_due, "The amount at the bottom of the bill.",
         _src("bill.amount_due", b_due))
    if paid:
        line("paid_so_far", "You have already paid", paid, "Payments you recorded for this bill.",
             _src("sum(payments.amount)", paid))
    if paid and b_due is not None and paid > b_due + TOL:
        flags.append(_flag("overpaid", "warning", "You may have paid too much",
                           f"You paid {money(paid)}, but the bill asks for {money(b_due)}. You may be owed a credit or refund.",
                           [_src("sum(payments.amount)", paid), _src("bill.amount_due", b_due)],
                           "Ask the billing office if you can get a refund or credit."))
    if b_due is None:
        missing.append({"field": "bill.amount_due", "why": "We need the amount the bill asks you to pay."})
    if _get(bill, "due_date") is None:
        missing.append({"field": "bill.due_date", "why": "Without a due date we cannot remind you."})

    expected_share: Decimal | None = None
    share_source = None

    if claim is None:
        flags.append(_flag("no_claim", "info", "No insurance explanation (EOB) is matched yet",
                           "Your insurer may not have finished with this bill. What you owe can change after it does.",
                           [_src("bill.claim_id", None)],
                           "Ask the provider if they sent a claim to your insurer. Ask your insurer for the EOB."))
        missing.append({"field": "claim", "why": "Add or match the insurer's EOB so we can check the bill."})
    else:
        c_status = _get(claim, "status")
        c_billed = D(_get(claim, "billed_amount"))
        allowed = D(_get(claim, "allowed_amount"))
        ins_paid = D(_get(claim, "insurer_paid"))
        ded, cop, coin = (D(_get(claim, k)) for k in ("deductible", "copay", "coinsurance"))
        resp = D(_get(claim, "patient_responsibility"))
        c_no = _get(claim, "claim_number")
        status_src = _src("claim.status", c_status)
        out["claim_number"] = c_no

        # ---- the money steps, in the order a patient reads an EOB ----
        line("claim_billed", "Provider billed the insurer", c_billed, GLOSSARY["Billed amount"],
             _src("claim.billed_amount", c_billed))
        discount = (c_billed - allowed) if (c_billed is not None and allowed is not None) else None
        line("discount", "Plan discount", discount,
             "The provider agreed not to charge this part. You do not owe it.",
             {"field": "claim.billed_amount - claim.allowed_amount",
              "value": None if discount is None else f"{discount:.2f}"})
        line("allowed", "Allowed amount", allowed, GLOSSARY["Allowed amount"], _src("claim.allowed_amount", allowed))
        line("insurer_paid", "Your insurer paid", ins_paid, GLOSSARY["Insurer paid"], _src("claim.insurer_paid", ins_paid))
        line("deductible", "Applied to your deductible", ded, GLOSSARY["Deductible"], _src("claim.deductible", ded))
        line("copay", "Your copay", cop, GLOSSARY["Copay"], _src("claim.copay", cop))
        line("coinsurance", "Your coinsurance", coin, GLOSSARY["Coinsurance"], _src("claim.coinsurance", coin))
        line("patient_responsibility", "Your share, says the insurer", resp, GLOSSARY["Patient responsibility"],
             _src("claim.patient_responsibility", resp))

        # ---- status ----
        if c_status in ("denied",):
            reason = _get(claim, "denial_reason")
            flags.append(_flag("claim_denied", "problem", "The insurer denied this claim",
                               "The claim was denied." + (f" Reason on the EOB: {reason}." if reason else " No reason was entered."),
                               [status_src, _src("claim.denial_reason", reason)],
                               "Ask your insurer how to appeal. Ask the provider not to send you to collections while you appeal."))
        elif c_status == "partially_denied":
            flags.append(_flag("claim_partially_denied", "warning", "The insurer covered only part of this claim",
                               "Some charges were not covered." + (f" Reason: {_get(claim, 'denial_reason')}." if _get(claim, "denial_reason") else ""),
                               [status_src, _src("claim.denial_reason", _get(claim, "denial_reason"))],
                               "Ask your insurer which charges were not covered and why."))
        elif c_status in ("processing", "submitted"):
            flags.append(_flag("claim_pending", "warning", "The insurer is not done yet",
                               "The claim is still being processed, so the final amount you owe is not known. "
                               "The bill may ask for more than your final share.",
                               [status_src],
                               "Ask the billing office if you can wait to pay until the EOB arrives."))
        elif c_status == "appealed":
            flags.append(_flag("claim_appealed", "info", "This claim is being appealed",
                               "The amounts may change when the appeal ends.", [status_src]))

        # ---- missing pieces (only matter once the insurer has decided) ----
        decided = c_status in ("paid", "partially_denied")
        if decided:
            for name, val in (("allowed_amount", allowed), ("insurer_paid", ins_paid),
                              ("patient_responsibility", resp)):
                if val is None:
                    missing.append({"field": f"claim.{name}", "why": "Copy this number from your EOB so we can check the math."})
            if ded is None and cop is None and coin is None:
                missing.append({"field": "claim.deductible / claim.copay / claim.coinsurance",
                                "why": "Enter each part of your share (use 0.00 if it does not apply)."})

        # ---- consistency checks ----
        if allowed is not None and c_billed is not None and allowed > c_billed + TOL:
            flags.append(_flag("allowed_exceeds_billed", "problem", "Allowed amount is higher than the price billed",
                               f"The allowed amount ({money(allowed)}) is more than the billed amount ({money(c_billed)}). "
                               "That is unusual. One of the numbers may be typed wrong.",
                               [_src("claim.allowed_amount", allowed), _src("claim.billed_amount", c_billed)],
                               "Check both numbers against the EOB."))

        parts = [x for x in (ded, cop, coin) if x is not None]
        parts_sum = sum(parts, ZERO) if parts else None
        if resp is not None and parts_sum is not None and not _near(parts_sum, resp):
            flags.append(_flag("components_dont_sum", "problem", "The parts of your share do not add up",
                               f"Deductible + copay + coinsurance = {money(parts_sum)}, but the EOB says your share is {money(resp)}. "
                               f"The difference is {money(abs(parts_sum - resp))}.",
                               [_src("claim.deductible", ded), _src("claim.copay", cop), _src("claim.coinsurance", coin),
                                _src("claim.patient_responsibility", resp)],
                               "Ask your insurer to explain the difference, or check for a typing mistake."))

        if allowed is not None and ins_paid is not None and resp is not None and not _near(allowed, ins_paid + resp):
            flags.append(_flag("allowed_not_paid_plus_share", "problem", "Insurer paid + your share does not equal the allowed amount",
                               f"The insurer paid {money(ins_paid)} and your share is {money(resp)}. Together that is "
                               f"{money(ins_paid + resp)}, but the allowed amount is {money(allowed)}.",
                               [_src("claim.insurer_paid", ins_paid), _src("claim.patient_responsibility", resp),
                                _src("claim.allowed_amount", allowed)],
                               "Ask your insurer why these do not match."))

        # your share, best available (stated > allowed-paid > parts)
        if resp is not None:
            expected_share, share_source = resp, _src("claim.patient_responsibility", resp)
        elif allowed is not None and ins_paid is not None:
            expected_share = allowed - ins_paid
            share_source = {"field": "claim.allowed_amount - claim.insurer_paid", "value": f"{expected_share:.2f}"}
        elif parts_sum is not None and decided:
            expected_share = parts_sum
            share_source = {"field": "claim.deductible + claim.copay + claim.coinsurance", "value": f"{parts_sum:.2f}"}
        if c_status in ("denied",):
            expected_share = None  # nothing reliable to compare against

        # ---- bill vs claim ----
        if b_billed is not None and c_billed is not None and not _near(b_billed, c_billed):
            flags.append(_flag("billed_mismatch", "warning", "The bill and the EOB list different prices",
                               f"The bill says {money(b_billed)}. The claim says {money(c_billed)}.",
                               [_src("bill.billed_amount", b_billed), _src("claim.billed_amount", c_billed)],
                               "Ask for an itemized bill and check the bill is for the same visit."))

        if expected_share is not None and b_due is not None:
            if b_due > expected_share + TOL:
                over = b_due - expected_share
                extra = ""
                if b_billed is not None and ins_paid is not None and _near(b_due, b_billed - ins_paid):
                    extra = " It looks like the plan discount was not applied: the bill is the full price minus what the insurer paid."
                flags.append(_flag("bill_exceeds_patient_share", "problem", "The bill asks for more than your insurer says you owe",
                                   f"The bill asks for {money(b_due)}. Your share on the EOB is {money(expected_share)}. "
                                   f"That is {money(over)} more.{extra}",
                                   [_src("bill.amount_due", b_due), share_source] +
                                   ([_src("claim.insurer_paid", ins_paid), _src("bill.billed_amount", b_billed)] if extra else []),
                                   "Ask the billing office to re-bill using the insurer's allowed amount. "
                                   "You can use the dispute button to keep notes."))
                if allowed is not None and b_due > allowed + TOL:
                    flags.append(_flag("bill_exceeds_allowed", "problem", "The bill asks for more than the allowed amount",
                                       f"The bill asks for {money(b_due)}, but the whole allowed amount for this service is only {money(allowed)}. "
                                       "A provider in your network should not ask you for more than your share.",
                                       [_src("bill.amount_due", b_due), _src("claim.allowed_amount", allowed)],
                                       "Ask the billing office and your insurer to check if this provider is in your network."))
            elif b_due + TOL < expected_share:
                flags.append(_flag("bill_below_patient_share", "info", "The bill is lower than your share on the EOB",
                                   f"The bill asks for {money(b_due)}. Your share on the EOB is {money(expected_share)}. "
                                   "A credit, adjustment or payment may have been applied. Keep an eye out for a second bill.",
                                   [_src("bill.amount_due", b_due), share_source]))
            if paid and paid > expected_share + TOL and b_due is not None and paid <= b_due + TOL:
                flags.append(_flag("paid_more_than_share", "warning", "You have paid more than your share on the EOB",
                                   f"You paid {money(paid)}. Your share on the EOB is {money(expected_share)}.",
                                   [_src("sum(payments.amount)", paid), share_source],
                                   "Ask for a refund or credit of the extra amount."))

        # ---- plan context ----
        if plan is not None:
            eff, end = _get(plan, "effective_date"), _get(plan, "end_date")
            sd = svc_date or _get(claim, "service_date")
            if sd and ((eff and sd < eff) or (end and sd > end)):
                flags.append(_flag("service_outside_coverage", "warning", "The service date is outside this plan's dates",
                                   "The date of service is not between the plan's start and end dates. "
                                   "Check you linked the right plan.",
                                   [_src("bill.service_date", sd), _src("plan.effective_date", eff), _src("plan.end_date", end)],
                                   "Check which plan was active on the service date."))
            plan_ded = D(_get(plan, "deductible_annual"))
            if plan_ded is not None and ded is not None and ded > plan_ded + TOL:
                flags.append(_flag("deductible_exceeds_plan", "warning", "Deductible on the claim is higher than the plan's yearly deductible",
                                   f"The claim applied {money(ded)} to your deductible. Your plan's yearly deductible is {money(plan_ded)}.",
                                   [_src("claim.deductible", ded), _src("plan.deductible_annual", plan_ded)],
                                   "Ask your insurer how much of your deductible has been used."))

    # ---------- duplicate service dates (bills and claims) ----------
    prov_key = _norm(provider)
    bill_id = _get(bill, "id")
    claim_id = _get(claim, "id") if claim is not None else None
    dup_b = [r for r in related_bills if _get(r, "id") != bill_id and _norm(_get(r, "provider_name")) == prov_key
             and svc_date is not None and _get(r, "service_date") == svc_date
             and _get(r, "status") != "void"]
    dup_c = [r for r in related_claims if _get(r, "id") != claim_id and _norm(_get(r, "provider_name")) == prov_key
             and svc_date is not None and _get(r, "service_date") == svc_date]
    if dup_b or dup_c:
        bits = [f"bill for {money(D(_get(r, 'amount_due')))}" for r in dup_b] + \
               [f"claim {_get(r, 'claim_number')}" for r in dup_c]
        flags.append(_flag("duplicate_service_date", "warning", "Another charge has the same provider and date",
                           f"There {'is' if len(bits) == 1 else 'are'} also {', '.join(bits)} for {provider} on "
                           f"{svc_date.isoformat() if svc_date else 'this date'}. It may be a separate service, or you may be billed twice.",
                           [_src("bill.provider_name", provider), _src("bill.service_date", svc_date)],
                           "Ask for an itemized bill to see if the same service is listed twice."))

    # ---------- result ----------
    out["lines"] = lines
    out["flags"] = flags
    out["missing_info"] = missing
    out["questions_to_ask"] = list(dict.fromkeys(f["ask"] for f in flags if f.get("ask")))
    out["expected_patient_share"] = None if expected_share is None else f"{expected_share:.2f}"
    out["expected_patient_share_source"] = share_source
    # what is still owed after payments, using the insurer's number when we have one
    if expected_share is not None:
        owes = max(expected_share - paid, ZERO)
        out["patient_owes_estimate"] = f"{owes:.2f}"
        out["patient_owes_basis"] = "your share on the EOB minus your payments"
    elif balance is not None:
        out["patient_owes_estimate"] = f"{max(balance, ZERO):.2f}"
        out["patient_owes_basis"] = "the bill minus your payments (not checked against an EOB)"
    else:
        out["patient_owes_estimate"] = None
        out["patient_owes_basis"] = "unknown"
    out["worst_severity"] = ("problem" if any(f["severity"] == "problem" for f in flags)
                             else "warning" if any(f["severity"] == "warning" for f in flags)
                             else "info" if flags else "ok")
    out["summary"] = _summary(provider, svc_date, b_due, paid, claim, expected_share, flags, out["patient_owes_estimate"])
    out["reading_grade_estimate"] = round(readability_grade(out["summary"]), 1)
    out["llm_note"] = None
    out["llm_enabled"] = llm_enabled()
    if llm_enabled():  # OFF by default
        try:
            text = _LLM({k: v for k, v in out.items() if k not in ("glossary",)})  # type: ignore[misc]
            out["llm_note"] = {"text": str(text)[:2000], "label": "Written by an AI helper. It can be wrong. "
                                                                    "The numbers above are the checked ones."}
        except Exception:  # pragma: no cover - never let an optional helper break the explanation
            log.exception("LLM explainer failed; continuing without it")
    return out


def _summary(provider, svc_date, b_due, paid, claim, share, flags, owes) -> str:
    when = f" on {svc_date.isoformat()}" if isinstance(svc_date, date) else ""
    parts = [f"This is a bill from {provider}{when}."]
    if b_due is not None:
        parts.append(f"The bill asks you to pay {money(b_due)}.")
    if claim is None:
        parts.append("We do not have an insurance explanation (EOB) for it yet, so we cannot check the amount.")
    else:
        st = _get(claim, "status")
        parts.append(f"The claim {STATUS_WORDS.get(st, 'has an unknown status')}.")
        if share is not None:
            parts.append(f"Your insurer says your share is {money(share)}.")
    problems = [f for f in flags if f["severity"] == "problem"]
    warns = [f for f in flags if f["severity"] == "warning"]
    if problems:
        n = len(problems)
        parts.append("We found 1 thing that looks wrong. See the list below." if n == 1
                     else f"We found {n} things that look wrong. See the list below.")
    elif warns:
        parts.append(f"We found {len(warns)} thing{'s' if len(warns) != 1 else ''} to double-check.")
    elif claim is not None:
        parts.append("The numbers add up.")
    if owes is not None and (claim is not None):
        parts.append(f"Our estimate of what you still owe is {money(D(owes))}.")
    parts.append("This is an estimate to help you ask questions. It is not legal or financial advice.")
    return " ".join(parts)
