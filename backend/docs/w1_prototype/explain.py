"""Deterministic, offline plain-language explanation of a bill + insurer EOB.

Uses only the numbers the patient entered. No LLM (an optional, off-by-default LLM mode is roadmap).
This is information, not billing or legal advice.
"""
from decimal import Decimal, ROUND_HALF_UP

TOL = Decimal("0.01")


def D(x) -> Decimal | None:
    if x is None or x == "":
        return None
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(d: Decimal | None) -> str:
    return "unknown" if d is None else f"${d:,.2f}"


def _f(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def explain(bill: dict, eob: dict | None) -> dict:
    flags: list[dict] = []
    b_billed, b_due, b_paid = D(bill.get("billed_amount")), D(bill.get("amount_due")), D(bill.get("payments_made")) or Decimal("0.00")
    balance = (b_due - b_paid) if b_due is not None else None
    result = {"has_eob": eob is not None, "bill_balance": float(balance) if balance is not None else None,
              "lines": [], "flags": flags, "patient_owes": None, "computed_patient_responsibility": None, "summary": ""}

    if b_paid > (b_due or Decimal("0")) and b_due is not None:
        flags.append(_f("overpaid", "warning", f"You've paid {money(b_paid)} but the bill asks for {money(b_due)}. You may be owed a refund or credit."))

    if eob is None:
        flags.append(_f("no_eob", "info", "No insurer explanation (EOB) is matched to this bill yet. "
                                          "The amount you owe can change after your insurer finishes processing the claim."))
        result["patient_owes"] = float(balance) if balance is not None else None
        result["lines"] = [
            {"key": "billed", "label": "Provider billed", "amount": float(b_billed) if b_billed is not None else None,
             "plain": "The full price the provider listed for this service before insurance."},
            {"key": "due", "label": "Provider is asking you to pay", "amount": float(b_due) if b_due is not None else None, "plain": "The amount on the bill."},
            {"key": "paid", "label": "You've already paid", "amount": float(b_paid), "plain": "Payments you recorded."},
            {"key": "balance", "label": "Balance left on the bill", "amount": result["bill_balance"], "plain": "Amount due minus what you've paid."},
        ]
        result["summary"] = (f"Your provider billed {money(b_billed)} and is asking you for {money(b_due)}. "
                             "Without your insurer's explanation (EOB) we can't check whether that is the right amount.")
        return result

    e_billed, allowed, ins_paid = D(eob.get("billed_amount")), D(eob.get("allowed_amount")), D(eob.get("insurer_paid"))
    ded, copay, coins = D(eob.get("deductible")), D(eob.get("copay")), D(eob.get("coinsurance"))
    stated_resp = D(eob.get("patient_responsibility"))
    parts = [x for x in (ded, copay, coins) if x is not None]
    computed_resp = sum(parts, Decimal("0.00")) if parts else None
    resp = stated_resp if stated_resp is not None else computed_resp
    result["computed_patient_responsibility"] = float(computed_resp) if computed_resp is not None else None
    adjustment = (e_billed - allowed) if (e_billed is not None and allowed is not None) else None

    # ---- missing info ----
    missing = [lab for lab, v in (("allowed amount", allowed), ("amount the insurer paid", ins_paid),
                                  ("deductible/copay/coinsurance (at least one, enter 0 if none)", computed_resp),
                                  ("amount you owe per the EOB", stated_resp)) if v is None]
    if missing:
        flags.append(_f("missing_info", "info", "Some numbers are missing from your entry: " + ", ".join(missing) +
                        ". Add them from your EOB for a more complete check."))

    # ---- consistency checks ----
    if e_billed is not None and allowed is not None and allowed > e_billed + TOL:
        flags.append(_f("allowed_exceeds_billed", "warning", f"The allowed amount ({money(allowed)}) is more than the billed amount ({money(e_billed)}). That is unusual - double-check the EOB numbers."))
    if allowed is not None and ins_paid is not None and resp is not None:
        diff = allowed - (ins_paid + resp)
        if abs(diff) > TOL:
            flags.append(_f("totals_dont_add_up", "warning",
                            f"The numbers don't add up: insurer paid {money(ins_paid)} + you owe {money(resp)} = {money(ins_paid + resp)}, "
                            f"but the allowed amount is {money(allowed)} (difference {money(abs(diff))}). Check for a typo, or ask your insurer to explain."))
    if stated_resp is not None and computed_resp is not None and abs(stated_resp - computed_resp) > TOL:
        flags.append(_f("responsibility_mismatch", "warning",
                        f"The EOB says you owe {money(stated_resp)}, but deductible + copay + coinsurance = {money(computed_resp)}."))
    if e_billed is not None and b_billed is not None and abs(e_billed - b_billed) > TOL:
        flags.append(_f("billed_mismatch", "warning", f"The provider bill says {money(b_billed)} was billed, but the EOB says {money(e_billed)}."))
    if resp is not None and b_due is not None:
        if b_due > resp + TOL:
            flags.append(_f("bill_exceeds_eob", "warning",
                            f"The provider is asking {money(b_due)} but your EOB says you owe {money(resp)} - that's {money(b_due - resp)} more. "
                            "Consider asking the provider's billing office for an itemized bill and how they got this number before paying the difference."))
        elif b_due < resp - TOL:
            flags.append(_f("bill_below_eob", "info", f"The provider is asking {money(b_due)}, less than the {money(resp)} your EOB says you owe. A later bill may follow."))
    if bill.get("service_date") and eob.get("service_date") and bill["service_date"] != eob["service_date"]:
        flags.append(_f("date_mismatch", "warning", f"Service dates differ: bill {bill['service_date']} vs EOB {eob['service_date']}."))
    if (bill.get("provider_name") or "").strip().lower() != (eob.get("provider_name") or "").strip().lower():
        flags.append(_f("provider_mismatch", "info", "The provider names on the bill and the EOB are different. Make sure they are for the same visit."))
    status = eob.get("claim_status")
    if status in ("denied", "partially_denied"):
        flags.append(_f("claim_denied", "warning", "Your insurer denied some or all of this claim. You may be able to appeal - check the EOB for the deadline."))
    elif status == "processing":
        flags.append(_f("claim_processing", "info", "The claim is still being processed, so these numbers may change."))

    lines = [
        {"key": "billed", "label": "Provider billed", "amount": _fl(e_billed), "plain": "The full list price of the service before insurance."},
        {"key": "adjustment", "label": "Discount your insurer negotiated", "amount": _fl(adjustment), "plain": "Billed minus allowed. In-network providers usually can't charge you for this part."},
        {"key": "allowed", "label": "Allowed amount", "amount": _fl(allowed), "plain": "What your insurer agreed the service is worth."},
        {"key": "insurer_paid", "label": "Insurer paid", "amount": _fl(ins_paid), "plain": "The portion your insurance covered."},
        {"key": "deductible", "label": "Went toward your deductible", "amount": _fl(ded), "plain": "Money you pay before your plan starts sharing costs."},
        {"key": "copay", "label": "Copay", "amount": _fl(copay), "plain": "A fixed fee for the visit."},
        {"key": "coinsurance", "label": "Coinsurance", "amount": _fl(coins), "plain": "Your percentage share after the deductible."},
        {"key": "patient_owes", "label": "You owe (per EOB)", "amount": _fl(resp), "plain": "What your insurer says is your share."},
        {"key": "bill_due", "label": "Provider is asking", "amount": _fl(b_due), "plain": "The amount on the provider's bill."},
        {"key": "paid", "label": "You've already paid", "amount": float(b_paid), "plain": "Payments you recorded."},
        {"key": "balance", "label": "Balance left on the bill", "amount": result["bill_balance"], "plain": "Amount the provider is asking minus what you've paid."},
    ]
    result["lines"] = [l for l in lines if l["amount"] is not None]
    result["patient_owes"] = _fl(resp)

    if resp is not None:
        s = (f"Of the {money(e_billed)} billed, your insurer allowed {money(allowed)}. "
             f"Insurance paid {money(ins_paid)} and your share is {money(resp)}.")
    else:
        s = f"Your provider billed {money(e_billed)}. Your EOB doesn't have enough numbers entered to work out your share."
    if any(f["severity"] == "warning" for f in flags):
        s += " Some numbers need a second look - see the flags below."
    else:
        s += " The numbers you entered are consistent."
    result["summary"] = s
    return result


def _fl(d):
    return float(d) if d is not None else None
