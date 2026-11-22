"""
classifier.py - Emergency Classifier (rule based).

Input : emergency type + severity (1 = mild ... 5 = critical)
Output: priority        1 (highest) ... 5 (lowest)
        required_resource    which unit the patient needs
        required_specialist  which doctor the patient needs
        est_handling_min     estimated dispatcher handling time (minutes)

est_handling_min is our answer to "what is the burst time of an SOS?":
SJF needs a burst time, which an emergency does not naturally have, so we
define it as the estimated time a dispatcher spends handling the case.
"""

EMERGENCY_TYPES = ["CARDIAC", "STROKE", "TRAUMA", "RESPIRATORY", "BURN",
                   "MATERNITY", "POISONING", "GENERAL"]

# type -> (default resource, specialist, base handling minutes, life-threatening?)
RULES = {
    "CARDIAC":     ("ICU_BED",           "CARDIOLOGIST",      8,  True),
    "STROKE":      ("ICU_BED",           "NEUROLOGIST",       8,  True),
    "TRAUMA":      ("OPERATION_THEATRE", "TRAUMA_SURGEON",    10, False),
    "RESPIRATORY": ("VENTILATOR",        "PULMONOLOGIST",     7,  True),
    "BURN":        ("ICU_BED",           "BURN_SPECIALIST",   9,  False),
    "MATERNITY":   ("GENERAL_BED",       "GYNECOLOGIST",      6,  False),
    "POISONING":   ("GENERAL_BED",       "GENERAL_PHYSICIAN", 5,  False),
    "GENERAL":     ("GENERAL_BED",       "GENERAL_PHYSICIAN", 4,  False),
}

PRIORITY_LABELS = {1: "Critical", 2: "Serious", 3: "Urgent", 4: "Standard", 5: "Minor"}


class ClassificationError(ValueError):
    pass


def classify(emergency_type, severity):
    """Return a dict with priority, required_resource, required_specialist, est_handling_min."""
    etype = str(emergency_type).strip().upper()
    if etype not in RULES:
        raise ClassificationError(f"Unknown emergency type '{emergency_type}'. "
                                  f"Choose one of: {', '.join(EMERGENCY_TYPES)}")
    try:
        sev = int(severity)
    except (TypeError, ValueError):
        raise ClassificationError("Severity must be a whole number from 1 to 5")
    if not 1 <= sev <= 5:
        raise ClassificationError("Severity must be between 1 (mild) and 5 (critical)")

    resource, specialist, base_minutes, life_threatening = RULES[etype]

    # Rule 1: severity sets the base priority (severity 5 -> priority 1)
    priority = 6 - sev
    # Rule 2: life-threatening conditions jump one level once they are at least moderate
    if life_threatening and sev >= 3:
        priority -= 1
    priority = max(1, min(5, priority))

    # Rule 3: resource escalation / de-escalation by severity
    if etype in ("CARDIAC", "STROKE", "RESPIRATORY") and sev <= 2:
        resource = "GENERAL_BED"             # mild case, observation bed is enough
    elif etype == "RESPIRATORY" and sev == 3:
        resource = "ICU_BED"                 # oxygen support, not yet a ventilator
    elif etype in ("MATERNITY",) and sev >= 4:
        resource = "OPERATION_THEATRE"       # complicated delivery
    elif etype in ("POISONING", "GENERAL") and sev >= 4:
        resource = "ICU_BED"
    elif etype == "TRAUMA" and sev <= 2:
        resource = "GENERAL_BED"             # minor injury, no surgery

    # Rule 4: handling time grows with severity (more coordination needed)
    est_minutes = base_minutes + sev

    return {
        "emergency_type": etype,
        "severity": sev,
        "priority": priority,
        "priority_label": PRIORITY_LABELS[priority],
        "required_resource": resource,
        "required_specialist": specialist,
        "est_handling_min": est_minutes,
    }


if __name__ == "__main__":
    for t in EMERGENCY_TYPES:
        for s in (1, 3, 5):
            c = classify(t, s)
            print(f"{t:12} sev {s} -> P{c['priority']} {c['required_resource']:18} "
                  f"{c['required_specialist']:18} {c['est_handling_min']} min")
