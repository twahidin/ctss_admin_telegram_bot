"""
Drive Agent — Natural language Google Drive management via Claude tool-use.

Usage:
    agent = DriveAgent()
    result = await agent.run("list files in Relief Committee")
"""

import json
import logging
from typing import Any

import anthropic
import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    CLAUDE_API_KEY,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    GOOGLE_DRIVE_ROOT_FOLDER_ID,
    APPS_SCRIPT_URL,
    APPS_SCRIPT_SECRET,
)

logger = logging.getLogger(__name__)

# ---------- Tool definitions for Claude ----------

TOOLS = [
    {
        "name": "list_folders",
        "description": "List folders inside a parent folder. If parent_folder_name is omitted, lists top-level folders in the shared Drive root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_folder_name": {
                    "type": "string",
                    "description": "Name of the parent folder to list sub-folders in. Omit for root.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_files",
        "description": "List files inside a folder (non-recursive). Returns file names, types, and IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_name": {
                    "type": "string",
                    "description": "Name of the folder to list files in.",
                },
            },
            "required": ["folder_name"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files by name across all folders. Returns matching files with their folder locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to match against file names.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the text content of a Google Doc or exported text from a file. For spreadsheets use read_spreadsheet instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Name of the file to read.",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Folder the file is in (helps disambiguate).",
                },
            },
            "required": ["file_name"],
        },
    },
    {
        "name": "read_spreadsheet",
        "description": "Read data from a Google Sheet. Returns rows as a table. Optionally specify a sheet tab name and range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Name of the Google Sheet.",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Folder the sheet is in (helps disambiguate).",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Tab/sheet name. Defaults to first sheet.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range, e.g. 'A1:D10'. Omit to read all data.",
                },
            },
            "required": ["file_name"],
        },
    },
    {
        "name": "write_spreadsheet",
        "description": "Write data to a Google Sheet. Can update a specific range or append rows at the bottom.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Name of the Google Sheet.",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Folder the sheet is in.",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Tab/sheet name. Defaults to first sheet.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 range to write to (e.g. 'A5:C5'). Required if mode is 'update'.",
                },
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": "2D array of values. Each inner array is one row.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "update"],
                    "description": "'append' adds rows at the bottom, 'update' writes to a specific range. Defaults to 'append'.",
                },
            },
            "required": ["file_name", "values"],
        },
    },
    {
        "name": "create_folder",
        "description": "Create a new folder inside a parent folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_name": {
                    "type": "string",
                    "description": "Name for the new folder.",
                },
                "parent_folder_name": {
                    "type": "string",
                    "description": "Parent folder to create inside. Omit for root.",
                },
            },
            "required": ["folder_name"],
        },
    },
    {
        "name": "delete_folder",
        "description": "Move a folder to trash (soft delete). Only works on empty or non-critical folders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_name": {
                    "type": "string",
                    "description": "Name of the folder to trash.",
                },
                "parent_folder_name": {
                    "type": "string",
                    "description": "Parent folder it lives in. Omit for root.",
                },
            },
            "required": ["folder_name"],
        },
    },
    {
        "name": "create_file",
        "description": "Create a new Google Sheet or Google Doc via the Apps Script bridge. The file will be owned by the Drive owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_type": {
                    "type": "string",
                    "enum": ["sheet", "doc"],
                    "description": "Type of file to create.",
                },
                "file_name": {
                    "type": "string",
                    "description": "Name for the new file.",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Folder to create the file in.",
                },
                "content": {
                    "type": "string",
                    "description": "Initial text content (for docs only).",
                },
            },
            "required": ["file_type", "file_name", "folder_name"],
        },
    },
    {
        "name": "sync_folder",
        "description": "Trigger a Drive-to-database sync for a folder, importing new files into the bot's daily entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_name": {
                    "type": "string",
                    "description": "Name of the folder to sync.",
                },
            },
            "required": ["folder_name"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a Google Drive assistant for a school admin Telegram bot. "
    "You help admins manage files and folders in the school's shared Google Drive. "
    "Use the provided tools to fulfil the user's request. "
    "Be concise in your final answer — use Telegram-friendly Markdown. "
    "When listing files or folders, use bullet points. "
    "If a tool returns an error, explain what went wrong clearly."
)


class DriveAgent:
    """Claude-powered agent for natural language Google Drive management."""

    def __init__(self):
        self.claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        # Parse service account credentials
        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )
        self.sa_email = sa_info.get("client_email", "")

        self.drive = build("drive", "v3", credentials=creds)
        self.sheets = build("sheets", "v4", credentials=creds)
        self.root_id = GOOGLE_DRIVE_ROOT_FOLDER_ID

    # ---------- public entry point ----------

    async def run(self, user_query: str) -> str:
        """Run the agent loop and return the final text response."""
        messages = [{"role": "user", "content": user_query}]

        for _ in range(8):  # max 8 tool turns
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # If the model is done (no tool use), return the text
            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            # Process tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await self._execute_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            if not tool_results:
                return self._extract_text(response)

            # Feed results back
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "I reached the maximum number of steps. Please try a simpler request."

    # ---------- tool dispatcher ----------

    async def _execute_tool(self, name: str, inp: dict) -> str:
        """Dispatch a tool call and return a JSON-serialisable string result."""
        try:
            handler = {
                "list_folders": self._tool_list_folders,
                "list_files": self._tool_list_files,
                "search_files": self._tool_search_files,
                "read_file": self._tool_read_file,
                "read_spreadsheet": self._tool_read_spreadsheet,
                "write_spreadsheet": self._tool_write_spreadsheet,
                "create_folder": self._tool_create_folder,
                "delete_folder": self._tool_delete_folder,
                "create_file": self._tool_create_file,
                "sync_folder": self._tool_sync_folder,
            }.get(name)

            if not handler:
                return json.dumps({"error": f"Unknown tool: {name}"})

            result = await handler(inp)
            return json.dumps(result, ensure_ascii=False)

        except HttpError as e:
            logger.error(f"Drive API error in tool {name}: {e}")
            return json.dumps({"error": f"Google API error: {e.reason}"})
        except Exception as e:
            logger.error(f"Error in tool {name}: {e}", exc_info=True)
            return json.dumps({"error": str(e)})

    # ---------- folder resolution helpers ----------

    def _find_folder(self, folder_name: str, parent_id: str | None = None) -> dict | None:
        """Find a folder by name (case-insensitive) under a parent."""
        parent = parent_id or self.root_id
        resp = (
            self.drive.files()
            .list(
                q=f"'{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files", []):
            if f["name"].lower() == folder_name.lower():
                return f
        return None

    def _resolve_folder(self, name: str | None, parent_name: str | None = None) -> dict | None:
        """Resolve a folder name, optionally scoped to a parent."""
        if not name:
            return None
        parent_id = None
        if parent_name:
            parent = self._find_folder(parent_name)
            if parent:
                parent_id = parent["id"]
        return self._find_folder(name, parent_id)

    def _find_file(self, file_name: str, folder_name: str | None = None) -> dict | None:
        """Find a file by name, optionally in a specific folder."""
        if folder_name:
            folder = self._find_folder(folder_name)
            if not folder:
                return None
            parent_id = folder["id"]
        else:
            parent_id = self.root_id

        # Search in the folder and its sub-folders
        return self._search_in_folder(file_name, parent_id)

    def _search_in_folder(self, file_name: str, folder_id: str) -> dict | None:
        """Recursively search for a file by name in a folder tree."""
        resp = (
            self.drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id, name, mimeType, modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files", []):
            if f["name"].lower() == file_name.lower():
                return f
            # Recurse into sub-folders
            if f["mimeType"] == "application/vnd.google-apps.folder":
                result = self._search_in_folder(file_name, f["id"])
                if result:
                    return result
        return None

    # ---------- tool implementations ----------

    async def _tool_list_folders(self, inp: dict) -> Any:
        parent_name = inp.get("parent_folder_name")
        parent_id = self.root_id
        if parent_name:
            parent = self._find_folder(parent_name)
            if not parent:
                return {"error": f"Folder '{parent_name}' not found."}
            parent_id = parent["id"]

        resp = (
            self.drive.files()
            .list(
                q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        folders = [{"name": f["name"], "id": f["id"]} for f in resp.get("files", [])]
        return {"folders": folders, "count": len(folders)}

    async def _tool_list_files(self, inp: dict) -> Any:
        folder_name = inp["folder_name"]
        folder = self._find_folder(folder_name)
        if not folder:
            return {"error": f"Folder '{folder_name}' not found."}

        resp = (
            self.drive.files()
            .list(
                q=f"'{folder['id']}' in parents and mimeType!='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name, mimeType, modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = []
        for f in resp.get("files", []):
            files.append({
                "name": f["name"],
                "type": self._friendly_mime(f.get("mimeType", "")),
                "modified": f.get("modifiedTime", ""),
                "id": f["id"],
            })
        return {"files": files, "count": len(files)}

    async def _tool_search_files(self, inp: dict) -> Any:
        query = inp["query"]
        resp = (
            self.drive.files()
            .list(
                q=f"name contains '{query}' and trashed=false",
                fields="files(id, name, mimeType, parents)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            )
            .execute()
        )
        files = []
        for f in resp.get("files", []):
            files.append({
                "name": f["name"],
                "type": self._friendly_mime(f.get("mimeType", "")),
                "id": f["id"],
            })
        return {"files": files[:20], "count": len(files)}

    async def _tool_read_file(self, inp: dict) -> Any:
        file = self._find_file(inp["file_name"], inp.get("folder_name"))
        if not file:
            return {"error": f"File '{inp['file_name']}' not found."}

        mime = file.get("mimeType", "")
        if "spreadsheet" in mime:
            return {"error": "This is a spreadsheet. Use read_spreadsheet instead."}

        # Export Google Docs as plain text
        if mime == "application/vnd.google-apps.document":
            content = (
                self.drive.files()
                .export(fileId=file["id"], mimeType="text/plain")
                .execute()
            )
            text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        else:
            # Download binary files — only attempt text decode
            content = self.drive.files().get_media(fileId=file["id"], supportsAllDrives=True).execute()
            try:
                text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
            except UnicodeDecodeError:
                return {"error": "File is binary and cannot be read as text. Only text files and Google Docs are supported."}

        # Truncate
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"
        return {"file_name": file["name"], "content": text}

    async def _tool_read_spreadsheet(self, inp: dict) -> Any:
        file = self._find_file(inp["file_name"], inp.get("folder_name"))
        if not file:
            return {"error": f"Sheet '{inp['file_name']}' not found."}

        sheet_name = inp.get("sheet_name", "")
        range_str = inp.get("range", "")

        # Build the range string
        if sheet_name and range_str:
            full_range = f"'{sheet_name}'!{range_str}"
        elif sheet_name:
            full_range = f"'{sheet_name}'"
        elif range_str:
            full_range = range_str
        else:
            full_range = ""

        try:
            if full_range:
                result = (
                    self.sheets.spreadsheets()
                    .values()
                    .get(spreadsheetId=file["id"], range=full_range)
                    .execute()
                )
            else:
                # Get first sheet name, then read all
                meta = self.sheets.spreadsheets().get(spreadsheetId=file["id"]).execute()
                first_sheet = meta["sheets"][0]["properties"]["title"]
                result = (
                    self.sheets.spreadsheets()
                    .values()
                    .get(spreadsheetId=file["id"], range=f"'{first_sheet}'")
                    .execute()
                )
        except HttpError as e:
            return {"error": f"Could not read sheet: {e.reason}"}

        rows = result.get("values", [])
        # Truncate to 100 rows
        truncated = len(rows) > 100
        rows = rows[:100]
        return {
            "file_name": file["name"],
            "rows": rows,
            "total_rows": len(rows),
            "truncated": truncated,
        }

    async def _tool_write_spreadsheet(self, inp: dict) -> Any:
        file = self._find_file(inp["file_name"], inp.get("folder_name"))
        if not file:
            return {"error": f"Sheet '{inp['file_name']}' not found."}

        values = inp["values"]
        mode = inp.get("mode", "append")
        sheet_name = inp.get("sheet_name", "")

        # Resolve sheet name if not provided
        if not sheet_name:
            meta = self.sheets.spreadsheets().get(spreadsheetId=file["id"]).execute()
            sheet_name = meta["sheets"][0]["properties"]["title"]

        if mode == "append":
            result = (
                self.sheets.spreadsheets()
                .values()
                .append(
                    spreadsheetId=file["id"],
                    range=f"'{sheet_name}'",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": values},
                )
                .execute()
            )
            updated = result.get("updates", {})
            return {
                "status": "success",
                "rows_appended": updated.get("updatedRows", len(values)),
                "range": updated.get("updatedRange", ""),
            }
        else:
            # Update mode — range is required
            range_str = inp.get("range")
            if not range_str:
                return {"error": "Range is required for update mode (e.g. 'A1:C3')."}
            full_range = f"'{sheet_name}'!{range_str}"
            result = (
                self.sheets.spreadsheets()
                .values()
                .update(
                    spreadsheetId=file["id"],
                    range=full_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": values},
                )
                .execute()
            )
            return {
                "status": "success",
                "updated_range": result.get("updatedRange", ""),
                "updated_cells": result.get("updatedCells", 0),
            }

    async def _tool_create_folder(self, inp: dict) -> Any:
        folder_name = inp["folder_name"]
        parent_name = inp.get("parent_folder_name")
        parent_id = self.root_id

        if parent_name:
            parent = self._find_folder(parent_name)
            if not parent:
                return {"error": f"Parent folder '{parent_name}' not found."}
            parent_id = parent["id"]

        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = (
            self.drive.files()
            .create(body=metadata, fields="id, name", supportsAllDrives=True)
            .execute()
        )
        return {"status": "success", "folder_name": created["name"], "id": created["id"]}

    async def _tool_delete_folder(self, inp: dict) -> Any:
        folder_name = inp["folder_name"]
        parent_name = inp.get("parent_folder_name")
        folder = self._resolve_folder(folder_name, parent_name)

        if not folder:
            return {"error": f"Folder '{folder_name}' not found."}

        # Soft delete — move to trash
        self.drive.files().update(
            fileId=folder["id"],
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
        return {"status": "success", "trashed_folder": folder["name"]}

    async def _tool_create_file(self, inp: dict) -> Any:
        if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
            return {"error": "Apps Script bridge is not configured. Cannot create files."}

        folder_name = inp["folder_name"]
        folder = self._find_folder(folder_name)
        if not folder:
            return {"error": f"Folder '{folder_name}' not found."}

        payload = {
            "secret": APPS_SCRIPT_SECRET,
            "action": "createSheet" if inp["file_type"] == "sheet" else "createDoc",
            "name": inp["file_name"],
            "folderId": folder["id"],
        }
        if inp.get("content"):
            payload["content"] = inp["content"]

        result = await self._call_apps_script(payload)
        if result.get("error"):
            return {"error": result["error"]}
        return {
            "status": "success",
            "file_name": inp["file_name"],
            "file_type": inp["file_type"],
            "id": result.get("fileId", ""),
            "url": result.get("url", ""),
        }

    async def _tool_sync_folder(self, inp: dict) -> Any:
        folder_name = inp["folder_name"]
        # This is a stub — actual sync requires the bot's sync_drive_folder method.
        # We return info so the Claude response can tell the user to use /sync instead.
        return {
            "note": f"Folder sync for '{folder_name}' should be triggered via the bot's /sync command. "
            "The /drive agent can read and write files but does not run the full import pipeline."
        }

    # ---------- Apps Script bridge ----------

    async def _call_apps_script(self, payload: dict) -> dict:
        """POST to the Apps Script web app and return the JSON response."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                APPS_SCRIPT_URL,
                json=payload,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return {"error": f"Apps Script returned HTTP {resp.status_code}"}
            return resp.json()

    # ---------- helpers ----------

    @staticmethod
    def _extract_text(response) -> str:
        """Pull text blocks from a Claude response."""
        parts = []
        for block in response.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts) if parts else "(No response)"

    @staticmethod
    def _friendly_mime(mime: str) -> str:
        """Convert MIME types to human-friendly names."""
        mapping = {
            "application/vnd.google-apps.spreadsheet": "Google Sheet",
            "application/vnd.google-apps.document": "Google Doc",
            "application/vnd.google-apps.presentation": "Google Slides",
            "application/vnd.google-apps.folder": "Folder",
            "application/pdf": "PDF",
            "image/png": "PNG Image",
            "image/jpeg": "JPEG Image",
            "text/plain": "Text File",
            "text/csv": "CSV",
        }
        return mapping.get(mime, mime)
