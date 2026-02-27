# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CTSS Admin Telegram Bot — a school admin info bot for managing daily relief schedules, events, and announcements at CTSS (Singapore). Features role-based access, Claude AI-powered queries, Google Drive sync, and PDF/image OCR.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Initialize database tables
python school_admin_bot/setup.py

# Validate config and connectivity
python school_admin_bot/test_config.py

# Run the bot (polling mode)
python main.py
```

Deployed on Railway (`Procfile: worker: python main.py`). PostgreSQL provided by Railway addon.

## Required Environment Variables

Set in `.env` (see `.env.example`):
- `TELEGRAM_TOKEN`, `CLAUDE_API_KEY`, `DATABASE_URL`, `SUPER_ADMIN_IDS` (comma-separated ints)
- Optional: `GOOGLE_DRIVE_ROOT_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON string), `STORAGE_PATH`

## Architecture

**Single-class bot** — `SchoolAdminBot` in `school_admin_bot/main.py` (~3400 lines) contains all Telegram handlers, AI integration, and file processing. Entry point: `main.py` at repo root calls `SchoolAdminBot().run()`.

### Key Modules

| File | Purpose |
|------|---------|
| `school_admin_bot/main.py` | All bot handlers, conversation flows, scheduled jobs, Claude API calls |
| `school_admin_bot/database.py` | PostgreSQL operations via `psycopg` (sync, not async). `dict_row` factory. JSONB content storage |
| `school_admin_bot/drive_sync.py` | Google Drive API integration — service account auth, shared drive support, file content extraction |
| `school_admin_bot/config.py` | Env var loading, constants (TAGS, PERIOD_TIMES, SYNC_SCHEDULE) |
| `school_admin_bot/setup.py` | Database table creation and migrations |

### Data Flow

```
Telegram user → Bot handler (role check) → Database / Claude API / Drive sync
Scheduled jobs → Daily purge (11 PM) | Relief reminders (every 60s) | Drive sync (per-folder schedule)
```

### Role System

Roles in ascending privilege: `viewer` → `relief_member` → `student_admin` → `admin` → `superadmin`. Every handler starts with a role guard clause. Superadmins can `/assume <role>` for testing, resolved via `effective_role` (SQL COALESCE with `role_assumptions` table).

### Conversation Handlers

Multi-step flows use `ConversationHandler` with numeric state constants (e.g., `SELECTING_TAG`, `AWAITING_CONTENT`, `AWAITING_CODE`). States are defined as module-level `range()` values at the top of `main.py`.

### File Processing Pipeline

1. **Images**: Base64 → Claude Vision API → extracted text
2. **PDFs**: PyMuPDF text extraction first; if sparse, falls back to page-by-page image OCR (max 5 pages text, 2 pages OCR)
3. **Relief data**: Claude parses extracted text → JSON array of relief entries → matched to registered users → relief reminders created

### Scheduled Jobs

Configured in `SchoolAdminBot.run()` via `job_queue`:
- Daily purge at 23:00 SGT (delete old entries)
- Relief reminder check every 60s (7am–5pm)
- Per-folder Drive sync at times defined in `config.SYNC_SCHEDULE`

### Database

PostgreSQL with `psycopg` (sync connections). Key tables: `users`, `daily_entries` (JSONB content), `daily_codes`, `relief_reminders`, `noshow_reports`, `drive_folders`, `folder_role_access`, `user_folder_access`. Deduplication for Drive files via `drive_file_id` column. Migrations handled by checking column/table existence before ALTER.

## Patterns to Follow

- All handlers are `async def` — use `await` for Telegram API and Claude calls
- Guard clause pattern: check user exists and has required role at handler start, return early if unauthorized
- Use `safe_send_message()` for Markdown messages — it falls back to plain text if parsing fails
- Content stored as JSONB in `daily_entries.content` with fields: `type`, `file_name`, `extracted_text`, `source`, `folder`, `drive_file_id`
- Drive-synced entries use `source: "google_drive_scheduled"` to distinguish from Telegram uploads
- Bot uses polling mode (not webhooks) with retry logic (3 attempts, exponential backoff)
