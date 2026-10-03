"""Very simple keyword routing hint for appointment requests. NOT a diagnosis, NOT triage."""
DISCLAIMER = "This is a general suggestion for who to see. It is not a diagnosis and not medical advice."

EMERGENCY = [
    "chest pain", "can't breathe", "cannot breathe", "trouble breathing", "shortness of breath", "stroke", "face drooping",
    "slurred speech", "unconscious", "severe bleeding", "overdose", "suicid", "kill myself", "want to die", "seizure",
]
RULES = [
    (["rash", "skin", "mole", "acne", "eczema"], "Dermatology (skin) or primary care"),
    (["heart", "palpitation", "blood pressure"], "Primary care or cardiology (heart)"),
    (["cough", "cold", "flu", "fever", "sore throat", "congestion"], "Primary care"),
    (["knee", "back pain", "joint", "shoulder", "sprain", "fracture"], "Primary care or orthopedics (bones and joints)"),
    (["sugar", "diabetes", "thyroid"], "Primary care or endocrinology (hormones)"),
    (["anxiety", "depress", "sleep", "stress", "mood"], "Primary care or behavioral health"),
    (["vision", "eye"], "Eye care"),
    (["stomach", "nausea", "vomit", "diarrhea", "abdominal"], "Primary care"),
]


def route_hint(reason: str, symptoms: str = "") -> dict:
    text = f"{reason or ''} {symptoms or ''}".lower()
    urgent = None
    if any(k in text for k in EMERGENCY):
        urgent = ("Some of what you wrote can be serious. If this is an emergency or you feel unsafe, call your local emergency number "
                  "(911 in the US) now, or call or text 988 for crisis support (US). Don't wait for an appointment.")
    suggestions = []
    for keys, label in RULES:
        if any(k in text for k in keys) and label not in suggestions:
            suggestions.append(label)
    if not suggestions:
        suggestions = ["Primary care (a good first stop when you're not sure)"]
    return {"disclaimer": DISCLAIMER, "urgent_warning": urgent, "suggestions": suggestions[:3]}
