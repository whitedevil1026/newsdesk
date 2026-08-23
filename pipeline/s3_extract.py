"""Stage 3 — full-text extraction.

Optional. Without it the pipeline still runs on feed blurbs, but summary
quality and claim verification both degrade, so it is on by default when
trafilatura is installed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import settings
from .models import Cluster
from .utils import log

try:
    import trafilatura
    AVAILABLE = True
except ImportError:                     # pipeline must survive without it
    AVAILABLE = False


def _extract(url: str, timeout: int) -> str:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return ""
    return trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    ) or ""


def run(clusters: list[Cluster], lead_only: bool = True) -> list[Cluster]:
    if not AVAILABLE:
        log("extract", "trafilatura not installed - using feed blurbs only")
        for cl in clusters:
            for art in cl.articles:
                art.body = art.summary_raw
        return clusters

    cfg = settings()
    timeout = cfg["harvest"]["timeout_seconds"]

    # Only the lead article of each cluster needs full text; the rest are
    # corroboration links, not summary inputs.
    targets = [cl.lead for cl in clusters] if lead_only else \
              [a for cl in clusters for a in cl.articles]

    log("extract", f"fetching {len(targets)} article bodies")
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_extract, a.url, timeout): a for a in targets}
        for fut in as_completed(futures):
            art = futures[fut]
            try:
                body = fut.result()
            except Exception as exc:
                log("extract", f"! {art.domain}: {exc}")
                body = ""
            art.body = body or art.summary_raw
            ok += bool(body)

    log("extract", f"{ok}/{len(targets)} full bodies, rest fell back to blurbs")
    return clusters
