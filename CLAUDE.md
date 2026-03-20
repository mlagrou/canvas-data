# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Personal Canvas LMS tracker. Pulls assignments, quizzes, module items, and announcements from the Canvas API, diffs against the previous snapshot to flag new items, saves everything to `data.json`, and pushes to GitHub. A static `index.html` dashboard (served via GitHub Pages) reads `data.json` and displays a filterable one-stop view.

## Environment Setup

```
pip install -r requirements.txt
```

Required `.env` file (gitignored):
```
CANVAS_API_URL=https://<institution>.instructure.com
CANVAS_API_KEY=<your_canvas_token>
```

## Commands

**Run sync locally** (fetches data + pushes to GitHub):
```bash
python fetch_canvas.py       # fetch only (GitHub Actions does the git push)
python sync_canvas.py        # git push utility (called manually if needed)
```

**Test API connection:**
```bash
python test_connection.py
```

## Architecture

- `fetch_canvas.py` — Main sync script. Loads previous `data.json`, fetches all content from Canvas (assignments with submission status, quizzes, module items, announcements), diffs to set `is_new`, saves updated JSON.
- `sync_canvas.py` — Git utility: `push_to_github(file, message)` stages, commits, pushes to `origin`.
- `index.html` — Static dashboard served via GitHub Pages. Fetches `data.json` directly, renders filterable views (What's New / Due Dates / Announcements / Modules / Everything) with submission status badges and course color-coding.
- `.github/workflows/sync.yml` — GitHub Actions workflow: runs `fetch_canvas.py` on a cron schedule (4×/day), commits and pushes `data.json` if changed. Uses `CANVAS_API_URL` and `CANVAS_API_KEY` as repo secrets.
- `data.json` — Output snapshot. Top-level keys: `last_updated`, `courses`, `items`. Each item has `type` (assignment/quiz/module_item/announcement), `is_new`, `due_at`, `submission_state`, `url`, `course_id`.

## Deployment (GitHub Pages)

1. Push repo to GitHub (can be public or private — Pages works on both with the right plan).
2. Add `CANVAS_API_URL` and `CANVAS_API_KEY` as repo secrets (Settings → Secrets → Actions).
3. Enable GitHub Pages: Settings → Pages → Branch: `main`, folder: `/root`.
4. The dashboard is live at `https://<username>.github.io/<repo>/`.
5. The Actions workflow auto-syncs 4×/day; trigger manually via Actions → "Sync Canvas Data" → Run workflow.

## data.json Item Schema

```json
{
  "type": "assignment | quiz | module_item | announcement",
  "id": "assignment_123",
  "course_id": 456,
  "course_name": "CS 101",
  "course_code": "CS101",
  "title": "Lab 1",
  "due_at": "2024-03-20T23:59:00Z",
  "submission_state": "submitted | unsubmitted | graded | pending_review | untaken",
  "submitted_at": "...",
  "grade": "95",
  "score": 95,
  "points_possible": 100,
  "url": "https://...",
  "is_new": true,
  "module_name": "Week 3",
  "item_type": "Page | File | ExternalUrl | Assignment | Quiz",
  "completed": false,
  "message": "announcement body text (stripped HTML, max 600 chars)"
}
```
