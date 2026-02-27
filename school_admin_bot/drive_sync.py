import json
import io
import uuid
import logging
from typing import List, Dict, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from config import GOOGLE_DRIVE_ROOT_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_JSON

logger = logging.getLogger(__name__)


class DriveSync:
    """Handle Google Drive operations"""

    def __init__(self):
        """Initialize Google Drive API client"""
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not configured")
        
        # Parse JSON from environment variable
        try:
            if isinstance(GOOGLE_SERVICE_ACCOUNT_JSON, str):
                service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            else:
                service_account_info = GOOGLE_SERVICE_ACCOUNT_JSON
        except json.JSONDecodeError:
            raise ValueError("Invalid GOOGLE_SERVICE_ACCOUNT_JSON format")

        # Create credentials
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive']
        )

        # Build Drive API service
        self.service = build('drive', 'v3', credentials=credentials)
        self.root_folder_id = GOOGLE_DRIVE_ROOT_FOLDER_ID

    def list_folders(self, parent_folder_id: Optional[str] = None) -> List[Dict]:
        """
        List all folders in a parent folder (or root if not specified)
        Returns list of folder dicts with: id, name, mimeType
        """
        if not parent_folder_id:
            parent_folder_id = self.root_folder_id

        try:
            folders = []
            page_token = None
            while True:
                kwargs = {
                    "q": f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    "fields": "nextPageToken, files(id, name, mimeType)",
                    "pageSize": 100,
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                results = self.service.files().list(**kwargs).execute()
                folders.extend(results.get('files', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            logger.info(f"Found {len(folders)} folders in {parent_folder_id}")
            return folders

        except HttpError as error:
            logger.error(f"Error listing folders: {error}")
            return []

    def get_folder_by_name(self, folder_name: str, parent_folder_id: Optional[str] = None) -> Optional[Dict]:
        """Find a folder by name"""
        folders = self.list_folders(parent_folder_id)
        for folder in folders:
            if folder['name'].lower() == folder_name.lower():
                return folder
        return None

    def list_files_in_folder(self, folder_id: str, recursive: bool = False) -> List[Dict]:
        """
        List all files in a folder
        If recursive=True, also includes files in subfolders
        Returns list of file dicts with: id, name, mimeType, size, parents (for folder tracking)
        """
        try:
            all_files = []
            page_token = None
            while True:
                kwargs = {
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, parents)",
                    "pageSize": 100,
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                results = self.service.files().list(**kwargs).execute()
                items = results.get('files', [])
                page_token = results.get('nextPageToken')
            
                for item in items:
                    mime_type = item.get('mimeType', '')
                    # If it's a folder and recursive, get files from it
                    if mime_type == 'application/vnd.google-apps.folder' and recursive:
                        subfolder_files = self.list_files_in_folder(item['id'], recursive=True)
                        all_files.extend(subfolder_files)
                    elif mime_type != 'application/vnd.google-apps.folder':
                        all_files.append(item)
            
                if not page_token:
                    break
            if not recursive:
                logger.info(f"Found {len(all_files)} files in folder {folder_id}")
            return all_files

        except HttpError as error:
            logger.error(f"Error listing files: {error}")
            return []
    
    def get_file_folder_path(self, file_id: str, root_folder_id: str) -> Optional[str]:
        """
        Get the folder path for a file relative to root folder
        Returns folder name or None if not in watched tree
        """
        try:
            file_metadata = self.service.files().get(
                fileId=file_id,
                fields='id, name, parents',
                supportsAllDrives=True,
            ).execute()
            
            parents = file_metadata.get('parents', [])
            if not parents:
                return None
            
            # Check if file is in root folder or its subfolders
            current_id = parents[0]
            folder_path = []
            
            # Walk up the folder tree
            max_depth = 10  # Prevent infinite loops
            depth = 0
            while current_id and depth < max_depth:
                if current_id == root_folder_id:
                    # We've reached the root, return the path
                    if folder_path:
                        # Return the immediate parent folder name
                        folder_info = self.service.files().get(
                            fileId=folder_path[-1],
                            fields='name',
                            supportsAllDrives=True,
                        ).execute()
                        return folder_info.get('name', 'Unknown')
                    else:
                        return None  # File is directly in root
                
                # Get parent folder info
                parent_info = self.service.files().get(
                    fileId=current_id,
                    fields='id, name, parents',
                    supportsAllDrives=True,
                ).execute()
                
                folder_path.append(current_id)
                parent_parents = parent_info.get('parents', [])
                current_id = parent_parents[0] if parent_parents else None
                depth += 1
            
            return None  # File not in watched tree
            
        except HttpError as error:
            logger.debug(f"Error getting file folder path: {error}")
            return None

    def download_file(self, file_id: str) -> Optional[bytes]:
        """Download a file by ID, returns file content as bytes"""
        try:
            request = self.service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            return file_content.getvalue()

        except HttpError as error:
            logger.error(f"Error downloading file {file_id}: {error}")
            return None

    def export_google_file(self, file_id: str, mime_type: str, export_format: str = 'application/pdf') -> Optional[bytes]:
        """
        Export a Google Docs/Sheets/Slides file
        export_format options:
        - 'application/pdf' for PDF
        - 'text/plain' for plain text
        - 'text/csv' for CSV (Google Sheets only)
        - 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' for Excel
        """
        try:
            # Map Google MIME types to export formats
            export_mime_map = {
                'application/vnd.google-apps.document': {
                    'pdf': 'application/pdf',
                    'txt': 'text/plain',
                },
                'application/vnd.google-apps.spreadsheet': {
                    'pdf': 'application/pdf',
                    'csv': 'text/csv',
                    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                },
                'application/vnd.google-apps.presentation': {
                    'pdf': 'application/pdf',
                },
            }

            if mime_type not in export_mime_map:
                logger.warning(f"Unsupported Google file type: {mime_type}")
                return None

            # Get export format
            if export_format == 'application/pdf':
                export_mime = export_mime_map[mime_type].get('pdf', 'application/pdf')
            elif export_format == 'text/plain':
                export_mime = export_mime_map[mime_type].get('txt', 'text/plain')
            elif export_format == 'text/csv':
                export_mime = export_mime_map[mime_type].get('csv', 'text/csv')
            else:
                export_mime = export_format

            request = self.service.files().export_media(fileId=file_id, mimeType=export_mime)
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            return file_content.getvalue()

        except HttpError as error:
            logger.error(f"Error exporting Google file {file_id}: {error}")
            return None

    def resolve_shortcut(self, file_id: str) -> Optional[Dict]:
        """
        Resolve a Google Drive shortcut to its target file
        Returns the target file info or None
        """
        try:
            file_metadata = self.service.files().get(
                fileId=file_id,
                fields='id, name, mimeType, shortcutDetails',
                supportsAllDrives=True,
            ).execute()
            
            # Check if it's a shortcut
            if file_metadata.get('mimeType') == 'application/vnd.google-apps.shortcut':
                shortcut_details = file_metadata.get('shortcutDetails', {})
                target_id = shortcut_details.get('targetId')
                
                if target_id:
                    # Get target file metadata
                    target_file = self.service.files().get(
                        fileId=target_id,
                        fields='id, name, mimeType',
                        supportsAllDrives=True,
                    ).execute()
                    logger.info(f"Resolved shortcut {file_metadata.get('name')} to target: {target_file.get('name')}")
                    return {
                        'target_file': target_file,
                        'shortcut_info': {
                            'id': file_id,
                            'name': file_metadata.get('name'),
                            'target_id': target_id
                        }
                    }
            
            return None
        except HttpError as error:
            logger.error(f"Error resolving shortcut {file_id}: {error}")
            return None

    def get_file_content(self, file: Dict) -> Optional[bytes]:
        """
        Get file content, handling both regular files and Google Docs/Sheets
        Also handles shortcuts by resolving them to their target files
        Returns bytes of file content
        """
        file_id = file['id']
        mime_type = file.get('mimeType', '')
        
        # Check if it's a shortcut - resolve to target file
        if mime_type == 'application/vnd.google-apps.shortcut':
            logger.info(f"Detected shortcut: {file.get('name')}, resolving to target file...")
            shortcut_result = self.resolve_shortcut(file_id)
            if shortcut_result:
                target_file = shortcut_result.get('target_file')
                # Recursively get content of target file
                return self.get_file_content(target_file)
            else:
                logger.warning(f"Could not resolve shortcut {file.get('name')}")
                return None

        # Check if it's a Google Workspace file
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            # Export Google Sheets as CSV for better structured data extraction
            logger.info(f"Exporting Google Sheets {file['name']} as CSV")
            return self.export_google_file(file_id, mime_type, 'text/csv')
        elif mime_type in [
            'application/vnd.google-apps.document',
            'application/vnd.google-apps.presentation'
        ]:
            # Export Docs/Presentations as PDF for processing
            logger.info(f"Exporting Google file {file['name']} as PDF")
            return self.export_google_file(file_id, mime_type, 'application/pdf')
        else:
            # Regular file, download directly
            logger.info(f"Downloading file {file['name']}")
            return self.download_file(file_id)

    def detect_file_category(self, file_name: str, folder_name: str) -> str:
        """
        Auto-detect category/tag based on filename and folder name
        Returns one of the TAGS from config
        """
        file_lower = file_name.lower()
        folder_lower = folder_name.lower()

        # Check folder name first
        if 'relief' in folder_lower:
            return 'RELIEF'
        elif 'absent' in folder_lower:
            return 'ABSENT'
        elif 'event' in folder_lower or 'bulletin' in folder_lower:
            return 'EVENT'
        elif 'venue' in folder_lower or 'room' in folder_lower:
            return 'VENUE_CHANGE'
        elif 'duty' in folder_lower or 'roster' in folder_lower:
            return 'DUTY_ROSTER'
        elif 'student' in folder_lower or 'movement' in folder_lower:
            return 'GENERAL'

        # Check filename
        if 'relief' in file_lower:
            return 'RELIEF'
        elif 'absent' in file_lower:
            return 'ABSENT'
        elif 'event' in file_lower:
            return 'EVENT'
        elif 'venue' in file_lower:
            return 'VENUE_CHANGE'
        elif 'duty' in file_lower or 'roster' in file_lower:
            return 'DUTY_ROSTER'

        # Default
        return 'GENERAL'

