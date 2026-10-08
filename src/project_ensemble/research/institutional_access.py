"""Explicit, meeting-local authorization for direct institutional source reading.

No credentials, proxies, paid search or authentication bypass are introduced.
The execution node must already have legitimate publisher access. Originals
remain private and failed attempts are cached across questions and resumes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from project_ensemble.research.documents import is_access_blocked_document


PRIVATE_ROOT = Path("human_private/institutional_documents")
_locks_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.Lock] = {}


def institutional_access_allowed(repo) -> bool:
    """Missing authorization denies access; frozen manifests must agree."""
    if repo is None:
        return False
    baseline = []
    for name in ("identity_private/meeting_manifest.json", "public/meeting_manifest.json"):
        path = repo.root / name
        if path.is_file():
            baseline.append(json.loads(path.read_text(encoding="utf-8")).get(
                "institutional_access_allowed", False))
    if any(type(value) is not bool for value in baseline) or len(set(baseline)) > 1:
        raise ValueError("机构访问授权记录无效或不一致；拒绝使用机构权限")
    allowed = baseline[0] if baseline else False
    for path in sorted((repo.root / "human_private/runtime_controls").glob("control-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("kind") == "institutional_access_allowed":
            if type(record.get("value")) is not bool or record.get("authority") != "HUMAN":
                raise ValueError("机构访问变更必须是人类明确授权的布尔值")
            allowed = record["value"]
    return allowed


def scholarly_candidate(candidate: dict) -> bool:
    return bool(candidate.get("doi") or candidate.get("retrieval_backend_id") == "openalex"
                or str(candidate.get("source_id", "")).startswith("https://openalex.org/"))


def _http_url(value) -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password):
        return url
    return None


def original_locations(candidate: dict, *, institutional: bool) -> list[tuple[str, bool]]:
    """Return (URL, restricted) without changing the retriever's OA assertion."""
    result = []
    public_url = _http_url(candidate.get("full_text_url"))
    if public_url and candidate.get("full_text_is_public"):
        result.append((public_url, False))
    if institutional and scholarly_candidate(candidate):
        provided = candidate.get("institutional_full_text_urls") or []
        if not isinstance(provided, list):
            provided = []
        doi = str(candidate.get("doi") or "").strip()
        if doi.startswith("10."):
            doi = "https://doi.org/" + doi
        for value in [candidate.get("full_text_url"), *provided, candidate.get("url"), doi]:
            url = _http_url(value)
            host = urlparse(url).hostname if url else None
            if url and host not in {"openalex.org", "api.openalex.org"} and all(url != old[0] for old in result):
                result.append((url, True))
    return result


class _PublisherPage(HTMLParser):
    """Find explicit PDF links and identifiable full-text article body only."""

    def __init__(self):
        super().__init__()
        self.pdf_links = []
        self.dois = []
        self.body_text = []
        self.body_depth = 0
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "meta":
            name = str(attrs.get("name") or attrs.get("property") or "").lower()
            if name in {"citation_pdf_url", "wkhealth_pdf_url"} and attrs.get("content"):
                self.pdf_links.append(attrs["content"])
            if name in {"citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi"}:
                self.dois.append(str(attrs.get("content") or ""))
        if tag == "a" and attrs.get("href"):
            path = urlparse(attrs["href"]).path.lower()
            if attrs.get("type") == "application/pdf" or path.endswith(".pdf") or "/doi/pdf/" in path or "/doi/epdf/" in path:
                self.pdf_links.append(attrs["href"])
        marker = " ".join(str(attrs.get(key) or "") for key in ("id", "class", "itemprop"))
        if self.body_depth:
            if tag not in {"meta", "link", "img", "br", "hr", "input", "source", "wbr"}:
                self.body_depth += 1
        elif re.search(r"(?:^|\s)(?:articleBody|article-body|article__body|fulltext|full-text|bodymatter)(?:\s|$)", marker, re.I):
            self.body_depth = 1

    def handle_endtag(self, tag):
        if tag in {"meta", "link", "img", "br", "hr", "input", "source", "wbr"}:
            return
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        if self.body_depth:
            self.body_depth -= 1

    def handle_data(self, data):
        if self.body_depth and not self.hidden:
            self.body_text.append(data)


def _doi(value: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).casefold()


def publisher_body_text(html: str) -> str:
    page = _PublisherPage()
    page.feed(html)
    return "\n".join(page.body_text).strip()


def fetch_institutional_original(repo, fetcher, candidate: dict) -> dict:
    """At most four direct GETs, no retries; cached success/failure per original."""
    if not institutional_access_allowed(repo):
        raise ValueError("institutional access is not authorized")
    locations = original_locations(candidate, institutional=True)
    digest = hashlib.sha256(json.dumps(
        [candidate["source_id"], candidate.get("doi"), locations, "institutional-v1"],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    relative = PRIVATE_ROOT / "attempts" / f"{digest}.json"
    with _locks_guard:
        lock = _locks.setdefault((str(repo.root.resolve()), digest), threading.Lock())
    with lock:
        path = repo.root / relative
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        record = {"source_id": candidate["source_id"], "status": "UNREADABLE", "attempts": [],
                  "record_path": str(relative), "read_at": datetime.now(timezone.utc).isoformat(),
                  "access_basis": None, "archived_path": None, "archived_sha256": None,
                  "media_type": None, "resolved_url": None}
        queue = list(locations)
        visited = set()
        blocked_hosts = set()
        while queue and len(record["attempts"]) < 4:
            url, restricted = queue.pop(0)
            if url in visited or urlparse(url).hostname in blocked_hosts:
                continue
            visited.add(url)
            attempt = {"url": url, "status": "UNREADABLE", "restricted": restricted}
            record["attempts"].append(attempt)
            try:
                document = fetcher.fetch(url)
                attempt["resolved_url"] = document.final_url
                if is_access_blocked_document(document.content, media_type=document.media_type,
                                              final_url=document.final_url):
                    attempt["status"] = "ACCESS_BLOCKED"
                    blocked_hosts.add(urlparse(document.final_url).hostname)
                    blocked_hosts.add(urlparse(url).hostname)
                    continue
                is_pdf = document.content.startswith(b"%PDF-")
                content = document.content
                media_type = "application/pdf" if is_pdf else document.media_type
                if not is_pdf:
                    if media_type not in {"text/html", "application/xhtml+xml"}:
                        continue
                    page = _PublisherPage()
                    page.feed(content.decode("utf-8-sig", errors="replace"))
                    expected = _doi(str(candidate.get("doi") or ""))
                    known_dois = {_doi(value) for value in page.dois if "10." in value}
                    if expected and known_dois and expected not in known_dois:
                        attempt["status"] = "IDENTITY_MISMATCH"
                        continue
                    for link in reversed(page.pdf_links[:8]):
                        target = _http_url(urljoin(document.final_url, link))
                        # Only explicit links hosted by this publisher page; no
                        # third-party download brokers, credential forwarding or guessed URLs.
                        if target and urlparse(target).hostname == urlparse(document.final_url).hostname:
                            queue.insert(0, (target, restricted))
                    if len(" ".join(page.body_text).strip()) < 1000:
                        attempt["status"] = "METADATA_ONLY"
                        continue
                content_digest = hashlib.sha256(content).hexdigest()
                original = PRIVATE_ROOT / "originals" / (content_digest + (".pdf" if is_pdf else ".html"))
                with _locks_guard:
                    content_lock = _locks.setdefault((str(repo.root.resolve()), content_digest), threading.Lock())
                with content_lock:
                    if not (repo.root / original).exists():
                        repo.docs.write_once(original, content)
                        if os.name != "nt":
                            (repo.root / original).chmod(0o600)
                    elif hashlib.sha256((repo.root / original).read_bytes()).hexdigest() != content_digest:
                        raise ValueError("institutional original hash mismatch")
                attempt["status"] = "FETCHED"
                record.update(status="FETCHED", access_basis=("INSTITUTIONAL_SUBSCRIPTION" if restricted else "OPEN_ACCESS"),
                              archived_path=str(original), archived_sha256=content_digest,
                              media_type=media_type, resolved_url=document.final_url)
                break
            except (httpx.HTTPError, OSError, ValueError) as exc:
                attempt["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 429}:
                    attempt["status"] = "ACCESS_BLOCKED"
                    blocked_hosts.add(urlparse(url).hostname)
        if record["status"] != "FETCHED" and any(item["status"] == "ACCESS_BLOCKED" for item in record["attempts"]):
            record["status"] = "ACCESS_BLOCKED"
        repo.docs.write_once(relative, json.dumps(record, ensure_ascii=False, indent=2))
        return record
