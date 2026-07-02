"""
graph.py
LangGraph pipeline for Agent 03. This is the core of why LangGraph was
chosen over a linear Express-style pipeline (per PRD architecture decision):
the quality-check step can route BACK to generation for a retry, which is
awkward to express as a straight-line sequence of API calls but natural as
a graph with conditional edges.

Pipeline:
  ingest -> extract -> generate -> quality_check -> (retry generate | curate) -> finalize
"""

from typing import TypedDict, Optional, Any
from langgraph.graph import StateGraph, END

import db
import ai_client
import ingestion

MAX_REGENERATION_ATTEMPTS = 2
CONTENT_TYPES = ["quiz", "flashcard", "summary", "exercise"]


class GraphState(TypedDict):
    job_id: str
    source_id: str
    organization_id: str
    course_id: Optional[str]
    source_type: str
    source_url: str
    requested_counts: Optional[dict]

    raw_text: str
    learning_objectives: list[str]
    key_concepts: list[str]

    # working content_type being generated/checked in the current loop pass
    current_content_type: str
    content_type_index: int
    generated_items: dict[str, list[dict]]  # content_type -> items
    quality_results: dict[str, dict]
    regeneration_attempts: dict[str, int]

    curated_references: list[dict]

    status: str
    current_step: str
    error: Optional[str]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def ingest_node(state: GraphState) -> GraphState:
    state["current_step"] = "ingest"
    try:
        raw_text = await ingestion.extract_text(state["source_type"], state["source_url"])
        if not raw_text or len(raw_text.strip()) < 20:
            raise ValueError("Extracted text is empty or too short - check the source file/URL")
        state["raw_text"] = raw_text
    except Exception as e:
        state["error"] = f"Ingestion failed: {e}"
        state["status"] = "failed"
    return state


async def extract_node(state: GraphState) -> GraphState:
    if state.get("error"):
        return state
    state["current_step"] = "extract"
    try:
        result = await ai_client.extract_learning_objectives(state["raw_text"])
        state["learning_objectives"] = result.get("learning_objectives", [])
        state["key_concepts"] = result.get("key_concepts", [])
    except Exception as e:
        state["error"] = f"Extraction failed: {e}"
        state["status"] = "failed"
    return state


async def generate_node(state: GraphState) -> GraphState:
    if state.get("error"):
        return state
    state["current_step"] = "generate"

    idx = state.get("content_type_index", 0)
    content_type = CONTENT_TYPES[idx]
    state["current_content_type"] = content_type

    try:
        result = await ai_client.generate_content_batch(
            content_type,
            state["learning_objectives"],
            state["key_concepts"],
            state["raw_text"],
            requested_counts=state.get("requested_counts"),
        )
        state.setdefault("generated_items", {})[content_type] = result.get("items", [])
        state.setdefault("quality_results", {})[content_type] = result.get("quality", {"passed": True, "score": None, "issues": []})
    except Exception as e:
        state["error"] = f"Generation failed for {content_type}: {e}"
        state["status"] = "failed"
        return state

    quality = state["quality_results"][content_type]
    attempts = state.setdefault("regeneration_attempts", {}).get(content_type, 0)

    if not quality.get("passed", True) and attempts < MAX_REGENERATION_ATTEMPTS:
        state["regeneration_attempts"][content_type] = attempts + 1
        state["next_route"] = "retry"
    else:
        if idx + 1 < len(CONTENT_TYPES):
            state["content_type_index"] = idx + 1
            state["next_route"] = "next_type"
        else:
            state["next_route"] = "curate"

    return state





def route_after_generate(state: GraphState) -> str:
    if state.get("error"):
        return "curate"
    return state.get("next_route", "curate")


async def curate_node(state: GraphState) -> GraphState:
    if state.get("error"):
        return state
    state["current_step"] = "curate"
    try:
        refs = await ai_client.curate_external_content(
            state["learning_objectives"], state["key_concepts"]
        )
        state["curated_references"] = refs
    except Exception as e:
        # Curation is supplementary - don't fail the whole pipeline over it
        print(f"[WARNING] Curation failed, continuing without external references: {e}")
        state["curated_references"] = []
    return state


def _normalize_payload(content_type: str, raw_payload: dict) -> dict:
    if not isinstance(raw_payload, dict):
        raw_payload = {"text": str(raw_payload)}
    
    payload = dict(raw_payload)
    
    if content_type == "flashcard":
        front = payload.get("front") or payload.get("term") or payload.get("concept") or payload.get("question") or payload.get("title") or payload.get("prompt") or "Flashcard Term"
        back = payload.get("back") or payload.get("definition") or payload.get("explanation") or payload.get("answer") or payload.get("details") or payload.get("text") or "Flashcard Definition"
        payload["front"] = front
        payload["back"] = back

    elif content_type == "summary":
        summary_text = payload.get("summary") or payload.get("overview") or payload.get("text") or payload.get("description") or payload.get("details") or "Summary Overview"
        takeaways = payload.get("key_takeaways") or payload.get("key_points") or payload.get("takeaways") or []
        if isinstance(takeaways, str):
            takeaways = [takeaways]
        payload["summary"] = summary_text
        payload["key_takeaways"] = takeaways

    elif content_type == "quiz":
        question = payload.get("question") or payload.get("title") or payload.get("prompt") or payload.get("text") or "Quiz Question"
        payload["question"] = question
        if "answer" not in payload and "correct_answer" in payload:
            payload["answer"] = payload["correct_answer"]

    elif content_type == "exercise":
        prompt_text = payload.get("prompt") or payload.get("question") or payload.get("task") or payload.get("scenario") or "Exercise Prompt"
        payload["prompt"] = prompt_text

    return payload


async def finalize_node(state: GraphState) -> GraphState:
    """Persists everything to Supabase: generated content (versioned,
    status=pending_review so an instructor must approve it) and curated
    references."""
    state["current_step"] = "finalize"

    if state.get("error"):
        db.update_source_status(state["source_id"], "failed", state["error"])
        state["status"] = "failed"
        return state

    try:
        for content_type, items in state.get("generated_items", {}).items():
            quality = state.get("quality_results", {}).get(content_type, {})
            version = db.get_next_version(state["source_id"], content_type)
            for item in items:
                raw_payload = item.get("payload", item)
                normalized_payload = _normalize_payload(content_type, raw_payload)
                db.insert_generated_content(
                    organization_id=state["organization_id"],
                    source_id=state["source_id"],
                    content_type=content_type,
                    payload=normalized_payload,
                    format=item.get("format", "na"),
                    bloom_level=item.get("bloom_level"),
                    quality_score=quality.get("score"),
                    version=version,
                    status="pending_review",
                )

        db.insert_curated_references(
            state["organization_id"], state["source_id"], state.get("curated_references", [])
        )

        db.update_source_status(state["source_id"], "ready")
        state["status"] = "ready"
    except Exception as e:
        state["error"] = f"Finalize/persist failed: {e}"
        state["status"] = "failed"
        db.update_source_status(state["source_id"], "failed", state["error"])

    return state


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("ingest", ingest_node)
    graph.add_node("extract", extract_node)
    graph.add_node("generate", generate_node)
    
    graph.add_node("curate", curate_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "extract")
    graph.add_edge("extract", "generate")
    graph.add_conditional_edges(
        "generate",
        route_after_generate,
        {
            "retry": "generate",
            "next_type": "generate",
            "curate": "curate",
        },
    )

    graph.add_edge("curate", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


async def run_pipeline(job_id: str, source_id: str, organization_id: str,
                        course_id: Optional[str], source_type: str, source_url: str,
                        requested_counts: Optional[dict] = None) -> GraphState:
    initial_state: GraphState = {
        "job_id": job_id,
        "source_id": source_id,
        "organization_id": organization_id,
        "course_id": course_id,
        "source_type": source_type,
        "source_url": source_url,
        "requested_counts": requested_counts or {"quiz": 5, "flashcard": 5, "summary": 1, "exercise": 2},
        "raw_text": "",
        "learning_objectives": [],
        "key_concepts": [],
        "current_content_type": "",
        "content_type_index": 0,
        "generated_items": {},
        "quality_results": {},
        "regeneration_attempts": {},
        "curated_references": [],
        "status": "processing",
        "current_step": "queued",
        "error": None,
    }
    graph = get_graph()
    final_state = await graph.ainvoke(initial_state, config={"recursion_limit": 50})
    return final_state


async def stream_pipeline(job_id: str, source_id: str, organization_id: str,
                           course_id: Optional[str], source_type: str, source_url: str,
                           requested_counts: Optional[dict] = None):
    initial_state: GraphState = {
        "job_id": job_id,
        "source_id": source_id,
        "organization_id": organization_id,
        "course_id": course_id,
        "source_type": source_type,
        "source_url": source_url,
        "requested_counts": requested_counts or {"quiz": 5, "flashcard": 5, "summary": 1, "exercise": 2},
        "raw_text": "",
        "learning_objectives": [],
        "key_concepts": [],
        "current_content_type": "",
        "content_type_index": 0,
        "generated_items": {},
        "quality_results": {},
        "regeneration_attempts": {},
        "curated_references": [],
        "status": "processing",
        "current_step": "queued",
        "error": None,
    }
    graph = get_graph()
    async for event in graph.astream(initial_state, config={"recursion_limit": 50}):
        node_name = list(event.keys())[0] if event else "queued"
        state = list(event.values())[0] if event else {}
        step = state.get("current_step", node_name)
        yield {"node": node_name, "step": step, "state": state}
