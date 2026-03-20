"""
Fetches courses, assignments, quizzes, module items, and announcements from
Canvas. Diffs against the previous data.json to flag new items, then saves
the updated snapshot.

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _load_previous() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Handle old format (plain list of courses)
        if isinstance(data, list):
            return {}
        return data
    return {}


def _build_old_ids(old: dict) -> set:
    return {item["id"] for item in old.get("items", [])}


def _safe_get(obj, attr, default=None):
    val = getattr(obj, attr, default)
    return val if val is not None else default


# ---------------------------------------------------------------------------
# Fetchers (one per content type)
# ---------------------------------------------------------------------------

def _fetch_assignments(course, course_meta: dict, old_ids: set) -> list:
    items = []
    try:
        for a in course.get_assignments(include=["submission"]):
            item_id = f"assignment_{a.id}"
            sub = getattr(a, "submission", {}) or {}
            if not isinstance(sub, dict):
                sub = vars(sub) if hasattr(sub, "__dict__") else {}
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
                "is_new": item_id not in old_ids,
            })
    except Exception as e:
        print(f"  [assignments] {course_meta['course_name']}: {e}")
    return items


def _fetch_quizzes(course, course_meta: dict, old_ids: set) -> list:
    items = []
    try:
        for q in course.get_quizzes():
            item_id = f"quiz_{q.id}"
            sub_state = "untaken"
            submitted_at = None
            score = None
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
                "is_new": item_id not in old_ids,
            })
    except Exception as e:
        print(f"  [quizzes] {course_meta['course_name']}: {e}")
    return items


def _fetch_modules(course, course_meta: dict, old_ids: set) -> list:
    items = []
    try:
        for module in course.get_modules():
            module_name = _safe_get(module, "name", "Unnamed Module")
            try:
                for mi in module.get_module_items(include=["content_details"]):
                    item_id = f"module_item_{mi.id}"
                    req = getattr(mi, "completion_requirement", None) or {}
                    if not isinstance(req, dict):
                        req = vars(req) if hasattr(req, "__dict__") else {}
                    completed = req.get("completed", False)
                    req_type = req.get("type")  # must_view, must_submit, must_contribute, etc.

                    items.append({
                        "type": "module_item",
                        "id": item_id,
                        **course_meta,
                        "title": _safe_get(mi, "title", "Unnamed Item"),
                        "module_name": module_name,
                        "item_type": _safe_get(mi, "type", "Unknown"),  # Page, File, ExternalUrl, Assignment, Quiz, Discussion
                        "due_at": None,
                        "completion_requirement": req_type,
                        "completed": completed,
                        "url": _safe_get(mi, "html_url", f"{API_URL}/courses/{course.id}/modules"),
                        "is_new": item_id not in old_ids,
                    })
            except Exception as e:
                print(f"    [module items] {module_name}: {e}")
    except Exception as e:
        print(f"  [modules] {course_meta['course_name']}: {e}")
    return items


def _fetch_announcements(course, course_meta: dict, old_ids: set) -> list:
    items = []
    try:
        for ann in course.get_discussion_topics(only_announcements=True, order_by="recent_activity", per_page=20):
            item_id = f"announcement_{ann.id}"
            raw_msg = _safe_get(ann, "message", "") or ""
            items.append({
                "type": "announcement",
                "id": item_id,
                **course_meta,
                "title": _safe_get(ann, "title", "Untitled"),
                "message": _strip_html(raw_msg)[:600],
                "posted_at": _safe_get(ann, "posted_at"),
                "due_at": None,
                "url": _safe_get(ann, "html_url", f"{API_URL}/courses/{course.id}/announcements"),
                "is_new": item_id not in old_ids,
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
    old_ids = _build_old_ids(old_data)

    courses_list = []
    all_items = []

    try:
        courses = list(canvas.get_courses(enrollment_type="student", enrollment_state="active"))
    except Exception as e:
        print(f"Failed to fetch courses: {e}")
        raise

    print(f"Found {len(courses)} active courses.")

    for course in courses:
        name = _safe_get(course, "name", "Unnamed Course")
        code = _safe_get(course, "course_code", "")
        print(f"\nFetching: {name}")

        course_meta = {
            "course_id": course.id,
            "course_name": name,
            "course_code": code,
        }
        courses_list.append({"id": course.id, "name": name, "code": code})

        all_items += _fetch_assignments(course, course_meta, old_ids)
        all_items += _fetch_quizzes(course, course_meta, old_ids)
        all_items += _fetch_modules(course, course_meta, old_ids)
        all_items += _fetch_announcements(course, course_meta, old_ids)

    new_count = sum(1 for i in all_items if i.get("is_new"))
    print(f"\nTotal: {len(all_items)} items ({new_count} new) across {len(courses_list)} courses.")

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "courses": courses_list,
        "items": all_items,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Saved -> {DATA_FILE}")
    return data


if __name__ == "__main__":
    fetch_all()
