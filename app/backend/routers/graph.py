import json
import re
from pathlib import Path
from fastapi import APIRouter, Depends
from ..dependencies import resolve_kb
from ..config import KBConfig

router = APIRouter(prefix="/api/graph", tags=["graph"])

_LINK_RE = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')


def _to_slug(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.strip('-') + '.md'


@router.get("/data")
def get_graph_data(kb: KBConfig = Depends(resolve_kb)):
    wiki_dir = Path(kb.wiki_dir)

    metrics_path = wiki_dir / "page_metrics.json"
    metrics: dict = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}

    nodes: dict[str, dict] = {}
    raw_edges: list[tuple[str, str]] = []

    for folder in ("entities", "concepts"):
        folder_path = wiki_dir / folder
        if not folder_path.exists():
            continue
        for md_file in sorted(folder_path.glob("*.md")):
            page_key = f"{folder}/{md_file.name}"
            content = md_file.read_text(encoding="utf-8", errors="ignore")

            title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
            title = title_match.group(1).strip('"\'') if title_match else md_file.stem.replace('-', ' ').title()

            m = metrics.get(page_key, {})
            nodes[page_key] = {
                "id": page_key,
                "label": title,
                "type": folder,
                "query_count": m.get("cds_query_count", 0),
                "gap_opens": m.get("gap_opens", 0),
                "persistent_gap": m.get("persistent_gap", False),
                "last_queried": m.get("last_queried"),
            }

            for link_text in _LINK_RE.findall(content):
                target_slug = _to_slug(link_text)
                for target_folder in ("entities", "concepts"):
                    target_key = f"{target_folder}/{target_slug}"
                    if (wiki_dir / target_folder / target_slug).exists():
                        if target_key != page_key:
                            raw_edges.append((page_key, target_key))
                        break

    # Deduplicate undirected edges
    seen: set[tuple] = set()
    edges = []
    for src, tgt in raw_edges:
        key = tuple(sorted([src, tgt]))
        if key not in seen:
            seen.add(key)
            edges.append({"source": src, "target": tgt})

    return {"nodes": list(nodes.values()), "edges": edges}
