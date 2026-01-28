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
            scopes=[
                'https://www.googleapis.com/auth/drive.readonly',
                'https://www.googleapis.com/auth/drive.metadata.readonly'
            ]
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
            results = self.service.files().list(
                q=f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name, mimeType)",
                pageSize=100
            ).execute()

            folders = results.get('files', [])
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

    def list_files_in_folder(self, folder_id: str) -> List[Dict]:
        """
        List all files in a folder
        Returns list of file dicts with: id, name, mimeType, size
        """
        try:
            results = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id, name, mimeType, size, modifiedTime)",
                pageSize=100
            ).execute()

            files = results.get('files', [])
            logger.info(f"Found {len(files)} files in folder {folder_id}")
            return files

        except HttpError as error:
            logger.error(f"Error listing files: {error}")
            return []

    def download_file(self, file_id: str) -> Optional[bytes]:
        """Download a file by ID, returns file content as bytes"""
        try:
            request = self.service.files().get_media(fileId=file_id)
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

    def get_file_content(self, file: Dict) -> Optional[bytes]:
        """
        Get file content, handling both regular files and Google Docs/Sheets
        Returns bytes of file content
        """
        file_id = file['id']
        mime_type = file.get('mimeType', '')

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

    def register_webhook(self, webhook_url: str, folder_id: str = None) -> Optional[Dict]:
        """
        Register a webhook to watch for changes in a folder
        Returns the webhook channel info
        """
        if not folder_id:
            folder_id = self.root_folder_id

        try:
            # Generate a unique channel ID
            import uuid
            channel_id = str(uuid.uuid4())
            
            # Register the webhook/watch
            request = self.service.files().watch(
                fileId=folder_id,
                body={
                    'id': channel_id,
                    'type': 'web_hook',
                    'address': webhook_url,
                }
            )
            
            result = request.execute()
            logger.info(f"Registered webhook for folder {folder_id}: {result.get('id')}")
            return result

        except HttpError as error:
            logger.error(f"Error registering webhook: {error}")
            return None

    def stop_webhook(self, channel_id: str, resource_id: str):
        """Stop a webhook channel"""
        try:
            self.service.channels().stop(
                body={
                    'id': channel_id,
                    'resourceId': resource_id
                }
            ).execute()
            logger.info(f"Stopped webhook channel {channel_id}")
        except HttpError as error:
            logger.error(f"Error stopping webhook: {error}")

    def get_changes(self, start_page_token: str = None) -> Dict:
        """
        Get list of changes since last sync
        Returns changes and new page token
        """
        try:
            if not start_page_token:
                # Get initial page token
                result = self.service.changes().getStartPageToken().execute()
                start_page_token = result.get('startPageToken')
                return {'changes': [], 'newStartPageToken': start_page_token}

            # Get changes
            result = self.service.changes().list(
                pageToken=start_page_token,
                fields='nextPageToken,newStartPageToken,changes(fileId,file(name,mimeType,parents))',
                pageSize=100
            ).execute()

            changes = result.get('changes', [])
            new_token = result.get('newStartPageToken')

            return {
                'changes': changes,
                'newStartPageToken': new_token
            }

        except HttpError as error:
            logger.error(f"Error getting changes: {error}")
            return {'changes': [], 'newStartPageToken': start_page_token}
