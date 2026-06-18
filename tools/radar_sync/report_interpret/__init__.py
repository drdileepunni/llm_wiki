"""Diagnostic-report interpretation: new scanned reports (echo, ECG, X-ray, CT, …)
→ multimodal interpretation in patient context → structured findings + informational card.

Sibling to `med_recon`: same watermark / download / image-host / audit / card-send
infrastructure, but interpretation instead of order reconciliation. In Phase 2 the
structured findings feed the live problem list via the delta → summary → problem-tracker
chain. See plan: report_interpret module."""
