# Email: IEC + Budget Approval Request

---

**To:** [IEC Chairperson] / [Finance / Engineering Leadership]  
**CC:** [Co-investigators, if any]  
**From:** Dr. Dileep Unni <dileep.unni@cloudphysician.net>  
**Subject:** Approval request — 1-week prospective study: LLM-based ICU alert system (Workspace 1A) + compute budget of ~$111 USD

**Attachment:** `study-protocol-v2-1week-1A.pdf`

---

Dear [Name],

I am writing to request two approvals for a 1-week prospective validation study of the LLM-based clinical decision support (CDS) system we have been running in Workspace 1A.

---

**What the system does**

The system monitors all admitted patients in Workspace 1A (currently ~30 patients across our partner ICUs). Once per hour, it reads each patient's vitals, labs, fluid balance, and clinical notes, and decides whether to fire an alert for any unresolved, worsening problem. Crucially, it suppresses alerts when the bedside team has already documented a management plan — this is the primary mechanism we believe differentiates it from existing rule-based tools.

The system has been running live in production. This study is the first prospective, structured evaluation of its diagnostic accuracy.

---

**What I am asking for**

**1. IEC waiver of consent**

The study is purely observational. The LLM pipeline is already in clinical use as a monitoring layer. No patient management protocol is changed. No new data is collected. Alerts reach clinicians through the existing Google Chat channel, and acting on them remains entirely at the clinician's discretion. We are only measuring whether the alerts the system was already generating correspond to real clinical events (as independently documented by bedside staff in the SBAR system).

I am requesting a waiver of informed consent on these grounds. The full protocol is attached.

**2. Compute cost approval: $111.30 USD (~₹9,300) for 7 days**

The system uses Google's Gemini 2.5 Flash API. I have attached real cost data from two live runs conducted today (22 May 2026):

| | Average per hourly run |
|---|---|
| Patients monitored | ~30 |
| Input tokens | 749,919 |
| Thinking tokens | 141,675 |
| **LLM cost** | **$0.55 USD** |

For the 7-day study (168 hourly runs):

| Item | Amount |
|---|---|
| Projected API cost (168 runs × $0.55) | $92.75 USD |
| 20% contingency (census variation) | $18.55 USD |
| **Total requested** | **$111.30 USD (~₹9,300)** |

The thinking tokens (~95% of cost) are the extended reasoning steps that allow the model to read clinical notes, assess whether problems are addressed, and suppress false alerts — the core of the system's clinical value. This is not a cost that can be reduced without compromising the reasoning quality we are trying to study.

For reference, if the system performs as expected and we scale to our full monitored network (~300 patients), the monthly API cost would be approximately $1,600–1,800 USD. The 1-week single-unit study is designed to generate publishable evidence before committing to that scale.

---

**Proposed study dates**

- **Data collection:** 26 May – 1 June 2026 (7 days, fully automated)
- **Adjudication:** 2–6 June 2026 (PI reviews alerts via web UI)
- **Submission target:** August 2026 (*npj Digital Medicine* / *Lancet Digital Health*)

---

**Why this matters**

There is no published prospective validation of a ReAct LLM agent as an ICU CDS tool in a real-world multi-hospital setting. This study would be, to our knowledge, the first. The automated continuous pipeline design means the data collection is zero-burden on clinical staff — it is running regardless. We are simply formalising the measurement.

The attached protocol covers the full study design, reference standard (High-urgency SBARs from BigQuery), statistical analysis plan (sensitivity/specificity with Wilson CIs), and ethical considerations.

I am happy to discuss at your convenience.

Best regards,  
Dr. Dileep Unni  
Cloudphysician  
dileep.unni@cloudphysician.net

---

*Attachment: study-protocol-v2-1week-1A.pdf (14 pages)*
