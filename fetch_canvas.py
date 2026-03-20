"""
Fetches courses, assignments, quizzes, module items, and announcements from
Canvas. On first run, fetches everything. On subsequent runs, only fetches
items that changed since the last sync and re-checks submission state for
unsubmitted items.

Uses Claude to summarize new announcements and extract/match dates back to
existing items.

Run locally:   python fetch_canvas.py
GitHub Actions handles the git commit/push automatically.
"""
import json
import os
import re
from datetime import datetime, timezone

from canvasapi import Canvas
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("CANVAS_API_URL", "").rstrip("/")
API_KEY = os.getenv("CANVAS_API_KEY", "")
DATA_FILE = "data.json"

try:
    import anthropic
    _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    HAS_AI = True
except Exception:
    _anthropic_client = None
    HAS_AI = False
    print("Note: anthropic not available. AI features disabled.")

EXCLUDE_TERMS = ["WI25"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _load_previous() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {}
        return data
    return {}


def _safe_get(obj, attr, default=None):
    val = getattr(obj, attr, default)
    return val if val is not None else default


def _is_excluded(course_name: str) -> bool:
    return any(term in course_name for term in EXCLUDE_TERMS)


def _sub_dict(sub) -> dict:
    """Normalize a submission object or dict to a plain dict."""
    if isinstance(sub, dict):
        return sub
    return vars(sub) if hasattr(sub, "__dict__") else {}


# ---------------------------------------------------------------------------
# AI announcement analysis
# ---------------------------------------------------------------------------

def _analyze_announcement(announcement: dict, course_items: list) -> dict:
    if not HAS_AI:
        return {"summary": [], "date_updates": [], "new_items": []}

    items_ctx = [
        {"id": item["id"], "title": item["title"], "type": item["type"], "due_at": item.get("due_at")}
        for item in course_items
        if item["type"] in ("assignment", "quiz")
    ]

    prompt = f"""You are analyzing a course announcement for a student dashboard.

Announcement title: {announcement['title']}
Announcement content: {announcement['message']}

Existing course items that might be referenced:
{json.dumps(items_ctx, indent=2)}

Respond with JSON only:
{{
  "summary": ["bullet 1", "bullet 2"],
  "date_updates": [
    {{
      "matched_id": "exact id from the items list above, or null",
      "matched_title": "exact title from the items list above, or null",
      "new_due_date": "ISO 8601 string or null",
      "note": "brief description of the change"
    }}
  ],
  "new_items": [
    {{
      "title": "task or event title",
      "due_date": "ISO 8601 string or null",
      "note": "context from the announcement"
    }}
  ]
}}

Rules:
- summary: as many bullets as needed to cover the key points
- date_updates: only when a specific date is mentioned AND it clearly matches an existing item
- new_items: only for specific tasks/dates that do not match any existing item
- If no dates or tasks are mentioned, return empty arrays for both
- Do not guess or fabricate information
- Current year context: {datetime.now().year}"""

    try:
        response = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1)
        return json.loads(text)
    except Exception as e:
        print(f"    [AI] Error: {e}")
        return {"summary": [], "date_updates": [], "new_items": []}


def _apply_ai_updates(merged: dict, date_updates: list, announcement: dict) -> None:
    for update in date_updates:
        matched_id = update.get("matched_id")
        new_due = update.get("new_due_date")
        if not matched_id or not new_due:
            continue
        if matched_id in merged and merged[matched_id].get("due_at") != new_due:
            merged[matched_id]["due_at"] = new_due
            merged[matched_id]["ai_updated"] = True
            merged[matched_id]["ai_note"] = update.get("note", "Due date updated via announcement")
            merged[matched_id]["ai_source"] = announcement["title"]


def _make_synthetic_items(new_items: list, course_meta: dict, announcement: dict, existing_ids: set) -> list:
    result = []
    for ni in new_items:
        if not ni.get("title"):
            continue
        slug = re.sub(r"\W+", "_", ni["title"])[:20]
        item_id = f"ann_item_{announcement['id']}_{slug}"
        result.append({
            "type": "announcement_item",
            "id": item_id,
            **course_meta,
            "title": ni["title"],
            "due_at": ni.get("due_date"),
            "note": ni.get("note", ""),
            "source_announcement": announcement["title"],
            "source_announcement_id": announcement["id"],
            "submission_state": None,
            "url": announcement["url"],
            "is_new": item_id not in existing_ids,
            "ai_extracted": True,
        })
    return result


# ---------------------------------------------------------------------------
# Submission refresh (targeted — only unsubmitted items)
# ---------------------------------------------------------------------------

def _refresh_assignment_submissions(course, merged: dict) -> None:
    """Re-check submission state only for unsubmitted assignments in this course."""
    targets = [
        item for item in merged.values()
        if item.get("course_id") == course.id
        and item["type"] == "assignment"
        and item.get("submission_state") in ("unsubmitted", "pending_review", None)
    ]
    for item in targets:
        assignment_id = item["id"].replace("assignment_", "")
        try:
            a = course.get_assignment(int(assignment_id), include=["submission"])
            sub = _sub_dict(getattr(a, "submission", {}) or {})
            item["submission_state"] = sub.get("workflow_state", item.get("submission_state"))
            item["submitted_at"] = sub.get("submitted_at", item.get("submitted_at"))
            item["grade"] = sub.get("grade", item.get("grade"))
            item["score"] = sub.get("score", item.get("score"))
        except Exception:
            pass


def _refresh_quiz_submissions(course, merged: dict) -> None:
    """Re-check submission state only for untaken/unknown quizzes in this course."""
    targets = [
        item for item in merged.values()
        if item.get("course_id") == course.id
        and item["type"] == "quiz"
        and item.get("submission_state") in ("untaken", "unknown", None)
    ]
    for item in targets:
        quiz_id = item["id"].replace("quiz_", "")
        try:
            q = course.get_quiz(int(quiz_id))
            subs = list(q.get_submissions())
            if subs:
                latest = subs[-1]
                item["submission_state"] = _safe_get(latest, "workflow_state", "untaken")
                item["score"] = _safe_get(latest, "score")
                item["submitted_at"] = _safe_get(latest, "finished_at")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fetchers — each accepts an optional `since` timestamp
# ---------------------------------------------------------------------------

def _fetch_assignments(course, course_meta: dict, existing_ids: set, since: str = None) -> list:
    items = []
    kwargs = {"include": ["submission"]}
    if since:
        kwargs["updated_since"] = since
    try:
        for a in course.get_assignments(**kwargs):
            item_id = f"assignment_{a.id}"
            sub = _sub_dict(getattr(a, "submission", {}) or {})
            items.append({
                "type": "assignment",
                "id": item_id,
                **course_meta,
                "title": _safe_get(a, "name", "Unnamed"),
                "due_at": _safe_get(a, "due_at"),
                "points_possible": _safe_get(a, "points_possible"),
                "submission_state": sub.get("workflow_state", "unsubmitted"),
                "submitted_at": sub.get("submitted_at"),
                "grade": sub.get("grade"),
                "score": sub.get("score"),
                "url": f"{API_URL}/courses/{course.id}/assignments/{a.id}",
                "is_new": item_id not in existing_ids,
                "ai_updated": False,
                "ai_note": None,
                "ai_source": None,
            })
    except Exception as e:
        print(f"  [assignments] {course_meta['course_name']}: {e}")
    return items


def _fetch_quizzes_full(course, course_meta: dict, existing_ids: set) -> list:
    """Full quiz fetch — only used on first run."""
    items = []
    try:
        for q in course.get_quizzes():
            item_id = f"quiz_{q.id}"
            sub_state, submitted_at, score = "untaken", None, None
            try:
                subs = list(q.get_submissions())
                if subs:
                    latest = subs[-1]
                    sub_state = _safe_get(latest, "workflow_state", "untaken")
                    submitted_at = _safe_get(latest, "finished_at")
                    score = _safe_get(latest, "score")
            except Exception:
                pass
            items.append({
                "type": "quiz",
                "id": item_id,
                **course_meta,
                "title": _safe_get(q, "title", "Unnamed Quiz"),
                "due_at": _safe_get(q, "due_at"),
                "points_possible": _safe_get(q, "points_possible"),
                "submission_state": sub_state,
                "submitted_at": submitted_at,
                "grade": None,
                "score": score,
                "url": f"{API_URL}/courses/{course.id}/quizzes/{q.id}",
                "is_new": item_id not in existing_ids,
                "ai_updated": False,
                "ai_note": None,
                "ai_source": None,
            })
    except Exception as e:
        print(f"  [quizzes] {course_meta['course_name']}: {e}")
    return items


def _fetch_modules(course, course_meta: dict, existing_ids: set, since: str = None) -> list:
    items = []
    try:
        for module in course.get_modules():
            # Skip modules that haven't been updated since last sync
            if since:
                updated_at = _safe_get(module, "updated_at")
                if updated_at and updated_at < since:
                    continue

            module_name = _safe_get(module, "name", "Unnamed Module")
            try:
                for mi in module.get_module_items(include=["content_details"]):
                    item_id = f"module_item_{mi.id}"
                    req = getattr(mi, "completion_requirement", None) or {}
                    if not isinstance(req, dict):
                        req = vars(req) if hasattr(req, "__dict__") else {}
                    items.append({
                        "type": "module_item",
                        "id": item_id,
                        **course_meta,
                        "title": _safe_get(mi, "title", "Unnamed Item"),
                        "module_name": module_name,
                        "item_type": _safe_get(mi, "type", "Unknown"),
                        "due_at": None,
                        "completion_requirement": req.get("type"),
                        "completed": req.get("completed", False),
                        "url": _safe_get(mi, "html_url", f"{API_URL}/courses/{course.id}/modules"),
                        "is_new": item_id not in existing_ids,
                        "position": _safe_get(mi, "id", 0),
                    })
            except Exception as e:
                print(f"    [module items] {module_name}: {e}")
    except Exception as e:
        print(f"  [modules] {course_meta['course_name']}: {e}")
    return items


def _fetch_announcements(course, course_meta: dict, existing_ids: set, since: str = None) -> list:
    items = []
    kwargs = {"only_announcements": True, "order_by": "recent_activity", "per_page": 20}
    if since:
        kwargs["updated_after"] = since
    try:
        for ann in course.get_discussion_topics(**kwargs):
            item_id = f"announcement_{ann.id}"
            raw_msg = _safe_get(ann, "message", "") or ""
            items.append({
                "type": "announcement",
                "id": item_id,
                **course_meta,
                "title": _safe_get(ann, "title", "Untitled"),
                "message": _strip_html(raw_msg)[:800],
                "summary": [],
                "posted_at": _safe_get(ann, "posted_at"),
                "due_at": None,
                "url": _safe_get(ann, "html_url", f"{API_URL}/courses/{course.id}/announcements"),
                "is_new": item_id not in existing_ids,
            })
    except Exception as e:
        print(f"  [announcements] {course_meta['course_name']}: {e}")
    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch_all() -> dict:
    canvas = Canvas(API_URL, API_KEY)
    old_data = _load_previous()
    last_sync = old_data.get("last_updated")
    is_first_run = last_sync is None

    existing_ids = {item["id"] for item in old_data.get("items", [])}
    ai_processed_ids = set(old_data.get("ai_processed_ids", []))

    # Start with all previous items, reset is_new so only truly new items are flagged
    merged: dict = {
        item["id"]: {**item, "is_new": False}
        for item in old_data.get("items", [])
    }

    try:
        courses = list(canvas.get_courses(enrollment_type="student", enrollment_state="active"))
    except Exception as e:
        print(f"Failed to fetch courses: {e}")
        raise

    courses = [c for c in courses if not _is_excluded(_safe_get(c, "name", ""))]
    mode = "full fetch" if is_first_run else f"incremental (since {last_sync[:16]})"
    print(f"Found {len(courses)} active courses. Mode: {mode}")

    courses_list = []

    for course in courses:
        name = _safe_get(course, "name", "Unnamed Course")
        code = _safe_get(course, "course_code", "")
        print(f"\nFetching: {name}")

        course_meta = {"course_id": course.id, "course_name": name, "course_code": code}
        courses_list.append({"id": course.id, "name": name, "code": code})

        # --- Assignments: fetch new/updated, then re-check unsubmitted ---
        new_assignments = _fetch_assignments(course, course_meta, existing_ids, since=last_sync)
        for item in new_assignments:
            # Preserve AI flags if we already had this item
            if item["id"] in merged:
                item["ai_updated"] = merged[item["id"]].get("ai_updated", False)
                item["ai_note"] = merged[item["id"]].get("ai_note")
                item["ai_source"] = merged[item["id"]].get("ai_source")
            merged[item["id"]] = item

        if not is_first_run:
            _refresh_assignment_submissions(course, merged)

        # --- Quizzes: full fetch on first run, then just refresh untaken ---
        if is_first_run:
            for item in _fetch_quizzes_full(course, course_meta, existing_ids):
                merged[item["id"]] = item
        else:
            _refresh_quiz_submissions(course, merged)

        # --- Modules: skip unchanged modules on incremental runs ---
        new_modules = _fetch_modules(course, course_meta, existing_ids, since=last_sync)
        for item in new_modules:
            merged[item["id"]] = item

        # --- Announcements: only new ones ---
        new_announcements = _fetch_announcements(course, course_meta, existing_ids, since=last_sync)

        # AI processing for new announcements
        for ann in new_announcements:
            if ann["id"] not in ai_processed_ids:
                if HAS_AI:
                    print(f"  [AI] Analyzing: {ann['title']}")
                    course_items = [v for v in merged.values() if v.get("course_id") == course.id]
                    result = _analyze_announcement(ann, course_items)
                    ann["summary"] = result.get("summary", [])
                    _apply_ai_updates(merged, result.get("date_updates", []), ann)
                    for synthetic in _make_synthetic_items(result.get("new_items", []), course_meta, ann, existing_ids):
                        merged[synthetic["id"]] = synthetic
                ai_processed_ids.add(ann["id"])

            merged[ann["id"]] = ann

    all_items = list(merged.values())
    new_count = sum(1 for i in all_items if i.get("is_new"))
    print(f"\nTotal: {len(all_items)} items ({new_count} new) across {len(courses_list)} courses.")

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "courses": courses_list,
        "items": all_items,
        "ai_processed_ids": list(ai_processed_ids),
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Saved -> {DATA_FILE}")
    return data


if __name__ == "__main__":
    fetch_all()
