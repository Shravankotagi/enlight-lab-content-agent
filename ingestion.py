"""
ingestion.py
Handles the "Document ingestion pipeline (PDF, video transcript, URL)"
capability from the PRD. Given a source_url and source_type, returns raw
text for the extraction node to work with.
"""

import re
import httpx
from pypdf import PdfReader
from io import BytesIO


async def fetch_bytes(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def parse_pdf_bytes(raw: bytes) -> str:
    """Extracts text page by page from raw PDF bytes."""
    reader = PdfReader(BytesIO(raw))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            # Strip trivial page numbers (e.g. "Page 1", "1 of 10", "1")
            if re.match(r"^(page\s+\d+(\s+of\s+\d+)?|\d+)$", s, re.IGNORECASE):
                continue
            lines.append(s)
        if lines:
            pages.append("\n".join(lines))
    
    full_text = "\n\n".join(pages).strip()
    if not full_text or len(full_text) < 10:
        raise ValueError("no extractable text — scanned PDFs not supported yet")
    return full_text


async def extract_pdf_text(source_url: str) -> str:
    """Downloads a PDF or text file from storage and extracts text page by page."""
    raw = await fetch_bytes(source_url)
    if raw.startswith(b"%PDF"):
        return parse_pdf_bytes(raw)
    
    # Pre-extracted text file
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text or len(text) < 10:
        raise ValueError("no extractable text — scanned PDFs not supported yet")
    return text


async def extract_video_transcript_text(source_url: str) -> str:
    """Video transcripts are expected to already be plain text/VTT/SRT files
    uploaded to storage (this service does not do audio transcription -
    that's the whisper server's job in a different part of the stack).
    Strips VTT/SRT timestamp lines if present, keeps just spoken text."""
    raw = await fetch_bytes(source_url)
    text = raw.decode("utf-8", errors="ignore")

    # Strip common VTT/SRT artifacts: cue numbers, timestamps, "WEBVTT" header
    lines = text.splitlines()
    cleaned = []
    timestamp_pattern = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT":
            continue
        if stripped.isdigit():
            continue
        if timestamp_pattern.match(stripped):
            continue
        cleaned.append(stripped)
    return "\n".join(cleaned)


async def extract_url_text(source_url: str) -> str:
    """Fetches a web page and does a naive HTML-tag strip. Good enough for
    article/syllabus pages; not a substitute for a full readability parser,
    but keeps this service dependency-light for the MVP."""
    raw = await fetch_bytes(source_url)
    html = raw.decode("utf-8", errors="ignore")
    # Strip scripts/styles first, then all remaining tags
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def extract_text(source_type: str, source_url: str) -> str:
    """Dispatches to the right extractor based on source_type."""
    if source_type == "pdf":
        return await extract_pdf_text(source_url)
    if source_type == "video_transcript":
        return await extract_video_transcript_text(source_url)
    if source_type == "url":
        return await extract_url_text(source_url)
    raise ValueError(f"Unknown source_type: {source_type}")
