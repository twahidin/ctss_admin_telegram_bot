"""
Test script to verify Google Drive write access for the service account.
Creates a test Google Sheet in the Relief Committee folder, then deletes it.
"""
import json
import os
import sys
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")


def main():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON not set")
        sys.exit(1)

    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    print(f"Service account: {service_account_info.get('client_email')}")

    # Use full drive scope (not readonly) for write access
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=credentials)

    # Step 1: Find the Relief Committee folder
    print(f"\nLooking for 'Relief Committee' folder under root {GOOGLE_DRIVE_ROOT_FOLDER_ID}...")
    results = service.files().list(
        q=f"'{GOOGLE_DRIVE_ROOT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    relief_folder = None
    for f in results.get('files', []):
        print(f"  Found folder: {f['name']} ({f['id']})")
        if f['name'].lower() == 'relief committee':
            relief_folder = f

    if not relief_folder:
        print("ERROR: Could not find 'Relief Committee' folder")
        sys.exit(1)

    print(f"\nRelief Committee folder ID: {relief_folder['id']}")

    # Step 2: Try creating a test Google Sheet
    print("\nAttempting to create a test Google Sheet...")
    try:
        file_metadata = {
            'name': 'TEST_WRITE_ACCESS - Delete Me',
            'mimeType': 'application/vnd.google-apps.spreadsheet',
            'parents': [relief_folder['id']],
        }
        created_file = service.files().create(
            body=file_metadata,
            fields='id, name, webViewLink',
            supportsAllDrives=True,
        ).execute()

        print(f"SUCCESS! Created: {created_file['name']}")
        print(f"  File ID: {created_file['id']}")
        print(f"  Link: {created_file.get('webViewLink', 'N/A')}")

        # Step 3: Clean up - delete the test file
        print("\nCleaning up - deleting test file...")
        service.files().delete(
            fileId=created_file['id'],
            supportsAllDrives=True,
        ).execute()
        print("Test file deleted successfully.")

        print("\n=== WRITE ACCESS VERIFIED ===")
        print("The service account can create and delete files in Relief Committee.")

    except HttpError as e:
        print(f"\nFAILED to create file: {e}")
        print(f"HTTP status: {e.resp.status}")
        print(f"Details: {e.content.decode()}")
        print("\nThis means the service account does NOT have write access.")
        print("Check that the folder permissions grant 'Editor' to:")
        print(f"  {service_account_info.get('client_email')}")
        sys.exit(1)


if __name__ == '__main__':
    main()
