"""
Page type templates for structured wiki pages.

Each template defines the canonical ## section headings for a page type.
These are injected into LLM write/fill prompts to ensure consistent structure,
which enables reliable section-level vector indexing and retrieval.
"""

TEMPLATES: dict[str, list[str]] = {
    "medication": [
        "Mechanism of Action",
        "Indications",
        "Dosing",
        "Renal / Hepatic Dose Adjustment",
        "Monitoring Parameters",
        "Adverse Effects",
        "Contraindications",
        "Drug Interactions",
    ],
    "investigation": [
        "Reference Range",
        "Clinical Significance",
        "Interpretation in ICU",
        "Common Causes of Abnormal Values",
        "Limitations",
    ],
    "procedure": [
        "Indications",
        "Contraindications",
        "Technique",
        "Complications",
        "Post-procedure Monitoring",
    ],
    "condition": [
        "Definition and Diagnostic Criteria",
        "Aetiology",
        "Clinical Features",
        "Investigations",
        "Management",
        "Monitoring",
        "Complications",
        "Prognosis",
    ],
    "default": [
        "Definition",
        "Clinical Significance",
        "Management",
        "Monitoring",
    ],
}

SUBTYPE_ENUM = ["medication", "investigation", "procedure", "condition", "default"]

SUBTYPE_DESCRIPTION = (
    "Page structural type. "
    "medication = drugs, biologics, infusions; "
    "investigation = labs, imaging, ECG, cultures; "
    "procedure = bedside procedures, interventions; "
    "condition = clinical syndromes, diseases, physiological states; "
    "default = anything else (protocols, targets, scoring tools)"
)


def get_template(subtype: str) -> list[str]:
    """Return section headings for a page type. Falls back to 'default'."""
    return TEMPLATES.get(subtype, TEMPLATES["default"])


def template_block(subtype: str) -> str:
    """
    Formatted instruction string to inject into LLM write/fill prompts.
    Tells the LLM which ## headings to use and to omit uncovered sections.
    """
    headings = get_template(subtype)
    lines = "\n".join(f"- {h}" for h in headings)
    return (
        f"This is a **{subtype}** page. "
        f"Use ONLY these ## section headings (omit any the source does not cover — "
        f"they will be tracked as knowledge gaps):\n{lines}"
    )


def inject_subtype_frontmatter(content: str, subtype: str) -> str:
    """
    Ensure `subtype: <value>` is present in the YAML frontmatter of a page.
    If a subtype line already exists, replaces it. Otherwise inserts after 'type:' line.
    No-op if content has no frontmatter block.
    """
    import re
    if not content.startswith("---"):
        return content

    # Replace existing subtype line
    if re.search(r"^subtype:", content, re.MULTILINE):
        return re.sub(r"^subtype:.*$", f"subtype: {subtype}", content, flags=re.MULTILINE)

    # Insert after 'type:' line if present
    if re.search(r"^type:", content, re.MULTILINE):
        return re.sub(
            r"^(type:.*?)$",
            rf"\1\nsubtype: {subtype}",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    # Insert before closing ---
    return re.sub(r"^(---\s*)$", rf"subtype: {subtype}\n\1", content, count=1, flags=re.MULTILINE)
