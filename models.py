"""
models.py
Pydantic models for Agent 03 - Content Generation & Curation Agent.

These mirror the Supabase schema (see schema.sql) and the PRD's defined
content types / formats / Bloom's Taxonomy levels.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums (match PRD "Key Capabilities" exactly)
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    pdf = "pdf"
    video_transcript = "video_transcript"
    url = "url"


class SourceStatus(str, Enum):
    uploaded = "uploaded"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class ContentType(str, Enum):
    quiz = "quiz"
    flashcard = "flashcard"
    summary = "summary"
    exercise = "exercise"


class ContentFormat(str, Enum):
    mcq = "mcq"
    open_ended = "open_ended"
    case_study = "case_study"
    # flashcards/summaries don't need a question format, so this is optional
    na = "na"


class BloomLevel(str, Enum):
    remember = "remember"
    understand = "understand"
    apply = "apply"
    analyze = "analyze"
    evaluate = "evaluate"
    create = "create"


class ReviewStatus(str, Enum):
    draft = "draft"                # just generated, not yet quality-checked
    pending_review = "pending_review"  # passed quality check, awaiting instructor
    approved = "approved"          # instructor approved, visible to learners
    rejected = "rejected"          # instructor rejected, needs regeneration


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    organization_id: str
    course_id: Optional[str] = None
    source_type: SourceType
    # For 'url' sources, this is the URL itself.
    # For 'pdf'/'video_transcript', this is a storage URL (e.g. Supabase Storage)
    # the file was already uploaded to - this service does not accept raw
    # file bytes over the API to keep payloads small; see README for the
    # upload-then-ingest flow.
    source_url: str
    filename: Optional[str] = None
    quiz_count: Optional[int] = 5
    flashcard_count: Optional[int] = 5
    summary_count: Optional[int] = 1
    exercise_count: Optional[int] = 2


class IngestResponse(BaseModel):
    job_id: str
    source_id: str
    status: SourceStatus


class JobStatusResponse(BaseModel):
    job_id: str
    source_id: str
    status: SourceStatus
    current_step: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Generated content
# ---------------------------------------------------------------------------

class GeneratedContentItem(BaseModel):
    id: Optional[str] = None
    organization_id: str
    source_id: str
    content_type: ContentType
    format: ContentFormat = ContentFormat.na
    bloom_level: Optional[BloomLevel] = None
    payload: dict[str, Any]  # actual question/flashcard/summary/exercise body
    quality_score: Optional[float] = None
    version: int = 1
    status: ReviewStatus = ReviewStatus.draft
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ReviewDecision(BaseModel):
    reviewer_id: str
    decision: ReviewStatus  # approved | rejected
    notes: Optional[str] = None


class BatchReviewRequest(BaseModel):
    reviewer_id: str
    decision: ReviewStatus  # approved | rejected
    content_ids: list[str]
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Curated external references
# ---------------------------------------------------------------------------

class CuratedReference(BaseModel):
    id: Optional[str] = None
    organization_id: str
    source_id: str
    title: str
    url: str
    description: Optional[str] = None
    relevance_score: Optional[float] = None
    added_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Pipeline state (internal - what flows through the LangGraph graph)
# ---------------------------------------------------------------------------

class PipelineState(BaseModel):
    job_id: str
    source_id: str
    organization_id: str
    course_id: Optional[str] = None
    source_type: SourceType
    source_url: str

    # Populated as the graph progresses
    raw_text: Optional[str] = None
    learning_objectives: list[str] = Field(default_factory=list)
    key_concepts: list[str] = Field(default_factory=list)

    generated_items: list[dict] = Field(default_factory=list)
    quality_failures: list[str] = Field(default_factory=list)
    regeneration_attempts: int = 0

    curated_references: list[dict] = Field(default_factory=list)

    status: SourceStatus = SourceStatus.processing
    current_step: Optional[str] = None
    error: Optional[str] = None
