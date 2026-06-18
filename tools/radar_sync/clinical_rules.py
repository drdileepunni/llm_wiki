"""Shared clinical reasoning rules used by more than one radar_sync model prompt.

These constants hold rule text that must stay identical across the status
classifier (status_classifier.py) and the problem tracker (problem_tracker.py).
Both prompts previously carried their own copy of this wording, which meant a
fix in one place silently drifted from the other. Editing the rule here keeps
the two prompts in sync.
"""

# Respiratory oxygenation — judge via the SpO2/FiO2 (SF) ratio, not raw SpO2.
# A falling SpO2 during a deliberate FiO2 wean is not deterioration.
RESPIRATORY_SF_RULE = (
    "For respiratory problems: NEVER judge SpO2 in isolation. get_vital_trend('SpO2') "
    "returns the SF ratio (SpO2 / FiO2%) alongside each reading. Use the SF ratio trend, "
    "not raw SpO2, to assess oxygenation. If FiO2 was reduced and the SF ratio is stable or "
    "improved, the SpO2 drop is planned weaning — treat as stable or improving, NOT worsening."
)

# Oliguria / anuria — a charted 0 ml of urine output is far more often a charting
# gap than true anuria. Require all three corroborating signals before believing it.
OLIGURIA_CHARTING_RULE = (
    "I/O charting in ICUs is frequently incomplete or entered retrospectively. Zero urine "
    "output in the chart — even across several consecutive hours — does NOT reliably indicate "
    "true anuria or oliguria. Always treat recorded 0 ml output as 'possible missed charting' "
    "unless ALL three of the following are true: (1) the daily total is also 0 ml, "
    "(2) clinical notes explicitly document anuria or oliguria, AND (3) creatinine is rising."
)
