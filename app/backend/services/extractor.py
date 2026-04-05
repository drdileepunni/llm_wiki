import fitz  # PyMuPDF
import httpx
from pathlib import Path

def extract_pdf(path: Path) -> str:
    doc = fitz.open(str(path))
    return "\n\n".join(page.get_text() for page in doc)

def extract_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def extract_pubmed(pmid: str) -> dict:
    """Fetch abstract + metadata from NCBI E-utilities. Returns dict with title, authors, abstract, citation."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    summary_url = f"{base}/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"
    abstract_url = f"{base}/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text"

    with httpx.Client(timeout=30) as client:
        summary = client.get(summary_url).json()
        abstract_text = client.get(abstract_url).text

    doc_summary = summary.get("result", {}).get(pmid, {})
    title = doc_summary.get("title", "Unknown Title")
    authors = ", ".join(a.get("name", "") for a in doc_summary.get("authors", [])[:5])
    pub_date = doc_summary.get("pubdate", "")
    journal = doc_summary.get("source", "")

    citation = f"{authors}. {title}. {journal}. {pub_date}. PMID: {pmid}"

    return {
        "title": title,
        "citation": citation,
        "text": abstract_text,
        "pmid": pmid,
    }

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path)
    return extract_markdown(path)
