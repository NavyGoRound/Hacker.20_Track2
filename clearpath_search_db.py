"""
ClearPath Search Database & Mini Search Engine (SQLite FTS5 + FastAPI)
- Uses SQLite FTS5 (BM25 ranking + highlighted snippets) for fast Google-like full-text search
- Stores collated open-source scholarship portals, public datasets, and plain-language guides
- Supports adding custom documents, importing JSON datasets, and crawling public web pages
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
import re
import sqlite3
from typing import List, Optional
from urllib.parse import urlparse
import urllib.request

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = os.getenv("CLEARPATH_SEARCH_DB", "clearpath_search.db")

app = FastAPI(
    title="ClearPath Search Engine",
    description="SQLite FTS5 full-text search database for open-source scholarship & portal data.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class IndexDocumentRequest(BaseModel):
    title: str
    url: str
    content: str
    category: str = "scholarship"
    source_name: str = "open_source"
    is_official: bool = False


class CrawlRequest(BaseModel):
    url: str
    category: str = "web"
    source_name: str = "open_web"
    is_official: bool = False


class _TextExtractor(HTMLParser):
    """Lightweight HTML-to-text & title extractor (no external dependencies)."""

    def __init__(self):
        super().__init__()
        self._skip = False
        self._in_title = False
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        cleaned = data.strip()
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        else:
            self.text_parts.append(cleaned)


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    """Create main documents table + FTS5 virtual search index + sync triggers."""
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'scholarship',
                source_name TEXT NOT NULL DEFAULT 'open_source',
                is_official INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                title,
                content,
                category,
                source_name,
                content='documents',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, title, content, category, source_name)
                VALUES (new.id, new.title, new.content, new.category, new.source_name);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, title, content, category, source_name)
                VALUES ('delete', old.id, old.title, old.content, old.category, old.source_name);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, title, content, category, source_name)
                VALUES ('delete', old.id, old.title, old.content, old.category, old.source_name);
                INSERT INTO documents_fts(rowid, title, content, category, source_name)
                VALUES (new.id, new.title, new.content, new.category, new.source_name);
            END;
            """
        )


def upsert_document(doc: IndexDocumentRequest, db_path: str = DB_PATH) -> int:
    domain = urlparse(doc.url).netloc.lower() or "local"
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO documents (title, url, domain, content, category, source_name, is_official, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                domain = excluded.domain,
                content = excluded.content,
                category = excluded.category,
                source_name = excluded.source_name,
                is_official = excluded.is_official,
                indexed_at = excluded.indexed_at
            """,
            (
                doc.title,
                doc.url,
                domain,
                doc.content,
                doc.category,
                doc.source_name,
                1 if doc.is_official else 0,
                now,
            ),
        )
        row = conn.execute("SELECT id FROM documents WHERE url = ?", (doc.url,)).fetchone()
        return row["id"]


def seed_open_source_data(db_path: str = DB_PATH) -> int:
    """Seed starter open-source scholarship & portal records if the DB is empty."""
    init_db(db_path)
    with get_conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        if count > 0:
            return count

    starter_docs = [
        IndexDocumentRequest(
            title="National Scholarship Portal (NSP) — Official Portal Guide",
            url="https://scholarships.gov.in/",
            content=(
                "Official Government of India National Scholarship Portal for central, state, and UGC "
                "scholarships. Requires Aadhaar verification, income certificate, caste/bonafide certificate, "
                "and active bank account seeded with Aadhaar."
            ),
            category="scholarship",
            source_name="gov_open_portal",
            is_official=True,
        ),
        IndexDocumentRequest(
            title="Karnataka SSP (State Scholarship Portal) — Pre-Matric & Post-Matric",
            url="https://ssp.postmatric.karnataka.gov.in/",
            content=(
                "Karnataka State Scholarship Portal for students in Karnataka. Supports e-Attestation, "
                "SATS ID, income and caste RD number verification, and bank DBT status tracking."
            ),
            category="scholarship",
            source_name="gov_open_portal",
            is_official=True,
        ),
        IndexDocumentRequest(
            title="Merit-cum-Means Scholarship Playbook — ClearPath",
            url="https://scholarship-portal.example/playbooks/merit",
            content=(
                "₹20,000 per year for students with strong academic marks and family income below the limit. "
                "Due 31 Dec. ClearPath flags pre-checked newsletter opt-in dark patterns and locks Aadhaar OTP as manual."
            ),
            category="playbook",
            source_name="clearpath_playbook",
            is_official=True,
        ),
        IndexDocumentRequest(
            title="Technical Education Scholarship Playbook — ClearPath",
            url="https://scholarship-portal.example/playbooks/tech",
            content=(
                "₹30,000 per year for engineering and diploma students. Due 15 Jan. Requires course details, "
                "income certificate PDF, and IFSC bank account details with per-task consent."
            ),
            category="playbook",
            source_name="clearpath_playbook",
            is_official=True,
        ),
        IndexDocumentRequest(
            title="First-Generation Learner Grant Playbook — ClearPath",
            url="https://scholarship-portal.example/playbooks/first",
            content=(
                "₹10,000 per year for the first graduate in a family. Due 20 Jan. Guided step-by-step form "
                "preparation with hard stop before final submit and zero-retention Privacy Receipt."
            ),
            category="playbook",
            source_name="clearpath_playbook",
            is_official=True,
        ),
        IndexDocumentRequest(
            title="How to Spot Fake Scholarship Portals & Dark Patterns",
            url="https://clearpath.example/guides/anti-scam",
            content=(
                "Always verify .gov.in or official university domains before entering Aadhaar, bank account, "
                "or OTPs. Never pay a registration fee to claim a government scholarship."
            ),
            category="safety",
            source_name="open_guide",
            is_official=False,
        ),
    ]
    for doc in starter_docs:
        upsert_document(doc, db_path)
    return len(starter_docs)


def _sanitize_fts_query(raw_query: str) -> str:
    """Convert user search text into a safe FTS5 prefix/OR query."""
    tokens = re.findall(r"[A-Za-z0-9_]+", raw_query)
    if not tokens:
        return ""
    return " OR ".join(f"{t}*" for t in tokens)


def search_documents(
    query: str,
    limit: int = 10,
    category: Optional[str] = None,
    official_only: bool = False,
    db_path: str = DB_PATH,
) -> List[dict]:
    """Run Google-style BM25-ranked full-text search with highlighted snippets."""
    fts_q = _sanitize_fts_query(query)
    if not fts_q:
        return []

    sql = """
        SELECT
            d.id,
            d.title,
            d.url,
            d.domain,
            d.category,
            d.source_name,
            d.is_official,
            snippet(documents_fts, 1, '<mark>', '</mark>', '...', 24) AS snippet,
            bm25(documents_fts, 5.0, 1.0, 2.0, 1.0) AS rank_score
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
    """
    params: list = [fts_q]

    if category:
        sql += " AND d.category = ?"
        params.append(category)
    if official_only:
        sql += " AND d.is_official = 1"

    # Note: SQLite FTS5 bm25() returns more-negative scores for better matches
    sql += " ORDER BY rank_score ASC LIMIT ?"
    params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "url": r["url"],
                "domain": r["domain"],
                "category": r["category"],
                "source_name": r["source_name"],
                "is_official": bool(r["is_official"]),
                "snippet": r["snippet"],
                "score": round(-float(r["rank_score"]), 4),
            }
            for r in rows
        ]


@app.on_event("startup")
def on_startup():
    seed_open_source_data(DB_PATH)


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    category: Optional[str] = None,
    official_only: bool = False,
):
    results = search_documents(q, limit=limit, category=category, official_only=official_only, db_path=DB_PATH)
    return {"query": q, "total": len(results), "results": results}


@app.post("/api/index")
def api_index_document(doc: IndexDocumentRequest):
    doc_id = upsert_document(doc, DB_PATH)
    return {"status": "indexed", "id": doc_id, "url": doc.url}


@app.post("/api/crawl")
def api_crawl_and_index(req: CrawlRequest):
    """Fetch a public webpage (requires outbound internet), extract text, and index into FTS5."""
    try:
        request = urllib.request.Request(
            req.url,
            headers={"User-Agent": "ClearPathSearchBot/1.0 (+open-source-indexer)"},
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {exc}")

    parser = _TextExtractor()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip() or req.url
    content = " ".join(parser.text_parts)
    content = re.sub(r"\s+", " ", content).strip()[:20000]

    if not content:
        raise HTTPException(status_code=422, detail="No readable text extracted from page.")

    doc_id = upsert_document(
        IndexDocumentRequest(
            title=title,
            url=req.url,
            content=content,
            category=req.category,
            source_name=req.source_name,
            is_official=req.is_official,
        ),
        DB_PATH,
    )
    return {"status": "crawled_and_indexed", "id": doc_id, "title": title, "url": req.url}