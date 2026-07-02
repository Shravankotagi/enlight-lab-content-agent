"""
ai_client.py
Gemini wrapper for content generation, quality checking, and web-search-based
curation. Mirrors the mock-fallback pattern used in risk-engine's
ai-service.js: if GEMINI_API_KEY is missing, returns template/mock output
instead of failing outright, so local dev works without a key.
"""

import os
import json
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

_HAS_KEY = bool(GEMINI_API_KEY)
_client = genai.Client(api_key=GEMINI_API_KEY) if _HAS_KEY else None


def _mock_warning(task: str) -> None:
    print(f"[WARNING] GEMINI_API_KEY not set. Returning MOCK output for: {task}. "
          f"Set GEMINI_API_KEY in .env / Railway Variables for real AI generation.")


async def extract_learning_objectives(raw_text: str) -> dict:
    """Returns {"learning_objectives": [...], "key_concepts": [...]}"""
    if not _HAS_KEY:
        _mock_warning("extract_learning_objectives")
        return {
            "learning_objectives": ["[MOCK] Understand the core topic of this document"],
            "key_concepts": ["[MOCK] concept A", "[MOCK] concept B"],
        }

    prompt = f"""You are analyzing course material to identify learning objectives and key concepts.

Return ONLY valid JSON (no markdown fences, no preamble) in this exact shape:
{{"learning_objectives": ["...", "..."], "key_concepts": ["...", "..."]}}

Extract 3-6 learning objectives and 5-10 key concepts from this material:

---
{raw_text[:12000]}
---
"""
    resp = _client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return _parse_json(resp.text)


async def generate_content_batch(content_type: str, learning_objectives: list[str],
                                  key_concepts: list[str], raw_text: str) -> dict:
    """Generates a batch of content items for one content_type AND a quality
    self-assessment in a single call (merged to reduce API calls).
    Returns: {"items": [...], "quality": {"passed": bool, "score": 0-100, "issues": [...]}}"""
    if not _HAS_KEY:
        _mock_warning(f"generate_content_batch:{content_type}")
        return {"items": _mock_content(content_type), "quality": {"passed": True, "score": 75.0, "issues": []}}

    format_guidance = {
        "quiz": "Generate 5 quiz questions. Mix formats: some 'mcq' (multiple choice "
                "with 4 options and one correct answer), some 'open_ended'. Tag each "
                "with a Bloom's Taxonomy level (remember, understand, apply, analyze, "
                "evaluate, create) matched to the question's cognitive demand.",
        "flashcard": "Generate 8 flashcards, each a concise term/concept on the front "
                     "and a clear definition/explanation on the back.",
        "summary": "Generate 1 structured summary of the material: a short overview "
                   "paragraph plus 4-6 bullet-point key takeaways.",
        "exercise": "Generate 2 practice exercises. At least one should be a "
                    "'case_study' format (a realistic scenario with an open-ended "
                    "prompt), tagged with an appropriate Bloom's level (typically "
                    "apply, analyze, or evaluate). Exercises must NOT be multiple-choice (MCQ) format. "
                    "They should be practical, open-ended activities, scenarios, or 'case_study' formats "
                    "requiring active application or analysis of the material.",
    }

    prompt = f"""You are generating {content_type} content for a course, based on the
material below. Learning objectives: {learning_objectives}. Key concepts: {key_concepts}.

{format_guidance.get(content_type, "")}

After generating, self-check your own output against this rubric:
- Accuracy: content must be factually consistent with the material, no fabricated claims.
- Coverage: content should collectively touch the learning objectives.
- Difficulty spread: for quizzes/exercises, cognitive levels should not all be the same.
Fail (passed: false) only for real accuracy problems or near-total lack of coverage.

Return ONLY valid JSON (no markdown fences, no preamble) in this exact shape:
{{"items": [{{"format": "mcq"|"open_ended"|"case_study"|"na", "bloom_level": "remember"|"understand"|"apply"|"analyze"|"evaluate"|"create"|null, "payload": {{...}}}}], "quality": {{"passed": true|false, "score": 0-100, "issues": ["...", "..."]}}}}

Source material:
---
{raw_text[:12000]}
---
"""
    resp = _client.models.generate_content(model=MODEL_NAME, contents=prompt)
    result = _parse_json(resp.text)
    if "items" not in result:
        result = {"items": result if isinstance(result, list) else [], "quality": {"passed": True, "score": None, "issues": []}}
    return result





async def curate_external_content(learning_objectives: list[str], key_concepts: list[str]) -> list[dict]:
    """Uses Gemini's web search tool to surface external articles/videos/case
    studies aligned to the learning objectives, per PRD 'External content
    curation via web search tool'."""
    if not _HAS_KEY:
        _mock_warning("curate_external_content")
        return [{
            "title": "[MOCK] Example external resource",
            "url": "https://example.com",
            "description": "Set GEMINI_API_KEY for real web-search-based curation.",
            "relevance_score": 0.5,
        }]

    try:
        prompt = f"""Find 3-5 high-quality external resources (articles, videos, or
case studies) that align with these learning objectives: {learning_objectives}
and key concepts: {key_concepts}.

After searching, respond with ONLY valid JSON (no markdown fences, no preamble):
a JSON array where each element has this shape:
{{"title": "...", "url": "...", "description": "...", "relevance_score": 0.0-1.0}}
"""
        # Web search grounding needs its own call without a forced JSON
        # response type, since the google_search tool and structured JSON
        # output can't be combined in one request. We ask for JSON in the
        # prompt text itself and parse leniently.
        resp = _client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        result = _parse_json(resp.text)
        return result if isinstance(result, list) else result.get("references", [])
    except Exception as e:
        print(f"[WARNING] Web search curation failed, returning empty list: {e}")
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str):
    """Gemini sometimes wraps JSON in markdown fences despite instructions
    not to. Strip those before parsing."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def _mock_content(content_type: str) -> list[dict]:
    mocks = {
        "quiz": [
            {"format": "mcq", "bloom_level": "understand", "payload": {
                "question": "[MOCK] Sample question?",
                "options": ["A", "B", "C", "D"], "correct_answer": "A"}},
        ],
        "flashcard": [
            {"format": "na", "bloom_level": None, "payload": {
                "front": "[MOCK] Term", "back": "[MOCK] Definition"}},
        ],
        "summary": [
            {"format": "na", "bloom_level": None, "payload": {
                "overview": "[MOCK] Summary overview.", "key_points": ["[MOCK] point 1"]}},
        ],
        "exercise": [
            {"format": "case_study", "bloom_level": "apply", "payload": {
                "scenario": "[MOCK] Scenario text.", "prompt": "[MOCK] What would you do?"}},
        ],
    }
    return mocks.get(content_type, [])

