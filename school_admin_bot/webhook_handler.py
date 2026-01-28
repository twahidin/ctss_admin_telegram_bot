"""
Google Drive Webhook Handler
Handles incoming webhook notifications from Google Drive
"""
import json
import logging
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from database import Database
from drive_sync import DriveSync
from config import WEBHOOK_SECRET, GOOGLE_DRIVE_ROOT_FOLDER_ID

logger = logging.getLogger(__name__)
db = Database()

# Initialize Flask app
webhook_app = Flask(__name__)

# Store bot instance for processing changes
bot_instance = None
drive_sync_instance = None


def set_bot_instance(bot, drive_sync):
    """Set the bot and drive_sync instances for processing changes"""
    global bot_instance, drive_sync_instance
    bot_instance = bot
    drive_sync_instance = drive_sync


@webhook_app.route('/webhook/drive', methods=['GET', 'POST'])
def handle_drive_webhook():
    """
    Handle Google Drive webhook notifications
    GET: Webhook verification (Google sends challenge)
    POST: Change notifications
    """
    if request.method == 'GET':
        # Webhook verification - Google sends a challenge
        challenge = request.args.get('challenge')
        if challenge:
            logger.info("Webhook verification challenge received")
            return challenge, 200
        return "OK", 200

    if request.method == 'POST':
        # Handle change notification
        try:
            data = request.get_json()
            
            # Verify webhook secret if configured
            if WEBHOOK_SECRET:
                # Google doesn't send a secret in the standard way, but we can verify the resource state
                pass

            # Get notification headers
            channel_id = request.headers.get('X-Goog-Channel-Id')
            resource_id = request.headers.get('X-Goog-Resource-Id')
            resource_state = request.headers.get('X-Goog-Resource-State')
            channel_token = request.headers.get('X-Goog-Channel-Token', '')

            logger.info(f"Webhook notification: channel_id={channel_id}, state={resource_state}")

            # Handle different resource states
            if resource_state == 'sync':
                # Initial sync - just acknowledge
                logger.info("Webhook sync notification received")
                return "OK", 200

            elif resource_state == 'update' or resource_state == 'change':
                # File changed - trigger sync
                logger.info("File change detected, triggering sync...")
                
                # Process changes asynchronously (don't block webhook response)
                if bot_instance and drive_sync_instance:
                    # Schedule sync in background
                    import threading
                    thread = threading.Thread(
                        target=process_drive_changes,
                        args=(channel_id,)
                    )
                    thread.daemon = True
                    thread.start()
                
                return "OK", 200

            elif resource_state == 'trash':
                # File deleted - we can ignore or handle cleanup
                logger.info("File deleted notification received")
                return "OK", 200

            else:
                logger.warning(f"Unknown resource state: {resource_state}")
                return "OK", 200

        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            return "Error", 500

    return "Method not allowed", 405


def process_drive_changes(channel_id):
    """Process drive changes triggered by webhook"""
    try:
        # Get webhook info from database
        webhook = None
        all_webhooks = db.get_all_active_webhooks()
        for w in all_webhooks:
            if w['channel_id'] == channel_id:
                webhook = w
                break

        if not webhook:
            logger.warning(f"Webhook channel {channel_id} not found in database")
            return

        folder_id = webhook['folder_id']
        page_token = webhook.get('page_token')

        # Get changes from Drive
        if not drive_sync_instance:
            logger.error("Drive sync instance not available")
            return

        changes_result = drive_sync_instance.get_changes(page_token)
        changes = changes_result.get('changes', [])
        new_page_token = changes_result.get('newStartPageToken')

        if not changes:
            logger.info("No changes detected")
            return

        logger.info(f"Processing {len(changes)} file changes")

        # Get folder info
        folder = db.get_folder_by_drive_id(folder_id)
        if not folder:
            logger.warning(f"Folder {folder_id} not found in database")
            return

        folder_name = folder['folder_name']
        
        # Process changed files
        files_to_sync = []
        for change in changes:
            file_info = change.get('file')
            if file_info:
                file_id = change.get('fileId')
                file_name = file_info.get('name', '')
                mime_type = file_info.get('mimeType', '')
                
                # Check if file is in our watched folder
                parents = file_info.get('parents', [])
                if folder_id in parents or file_id == folder_id:
                    files_to_sync.append({
                        'id': file_id,
                        'name': file_name,
                        'mimeType': mime_type
                    })

        if files_to_sync:
            # Sync the changed files
            sync_user_id = None
            if bot_instance and hasattr(bot_instance, 'app'):
                # Get first superadmin for system sync
                from config import SUPER_ADMIN_IDS
                if SUPER_ADMIN_IDS:
                    sync_user_id = SUPER_ADMIN_IDS[0]

            if sync_user_id:
                sync_changed_files(files_to_sync, folder, sync_user_id)

        # Update page token
        if new_page_token:
            db.update_webhook_page_token(channel_id, new_page_token)

    except Exception as e:
        logger.error(f"Error processing drive changes: {e}", exc_info=True)


# Store analysis functions
analyze_image_func = None
analyze_pdf_func = None


def set_analysis_functions(analyze_image, analyze_pdf):
    """Set the analysis functions from bot instance"""
    global analyze_image_func, analyze_pdf_func
    analyze_image_func = analyze_image
    analyze_pdf_func = analyze_pdf


def sync_changed_files(files, folder, sync_user_id):
    """Sync specific changed files"""
    try:
        total_processed = 0
        errors = []

        for file in files:
            try:
                # Get file content
                file_content = drive_sync_instance.get_file_content(file)
                
                if not file_content:
                    errors.append(f"{file['name']}: Failed to download")
                    continue

                # Detect category
                category = drive_sync_instance.detect_file_category(file['name'], folder['folder_name'])

                # Process based on file type
                extracted_text = ""
                file_type = "document"

                if file.get('mimeType', '').startswith('image/'):
                    if analyze_image_func:
                        extracted_text = analyze_image_func(file_content, category)
                    else:
                        extracted_text = f"[Image: {file['name']}]"
                    file_type = "photo"
                elif file.get('mimeType', '') == 'application/pdf' or file['name'].lower().endswith('.pdf'):
                    if analyze_pdf_func:
                        extracted_text = analyze_pdf_func(file_content, category)
                    else:
                        extracted_text = f"[PDF: {file['name']}]"
                    file_type = "document"
                elif file.get('mimeType', '') == 'application/vnd.google-apps.spreadsheet':
                    try:
                        extracted_text = file_content.decode('utf-8')
                    except:
                        extracted_text = file_content.decode('latin-1')
                    file_type = "document"
                elif file.get('mimeType', '').startswith('text/'):
                    try:
                        extracted_text = file_content.decode('utf-8')
                    except:
                        extracted_text = file_content.decode('latin-1')
                    file_type = "document"
                else:
                    if file_content[:4] == b'%PDF':
                        if analyze_pdf_func:
                            extracted_text = analyze_pdf_func(file_content, category)
                        else:
                            extracted_text = f"[PDF: {file['name']}]"
                    else:
                        try:
                            extracted_text = file_content.decode('utf-8')
                        except:
                            extracted_text = f"[Binary file: {file['name']}]"

                # Save to database
                content_data = {
                    "type": file_type,
                    "file_name": file['name'],
                    "extracted_text": extracted_text,
                    "source": "google_drive_webhook",
                    "folder": folder['folder_name'],
                }

                db.add_entry(sync_user_id, category, content_data)
                total_processed += 1

            except Exception as e:
                logger.error(f"Error processing file {file.get('name', 'unknown')}: {e}")
                errors.append(f"{file.get('name', 'unknown')}: {str(e)}")

        # Update sync time
        db.update_folder_sync_time(folder['id'])

        # Log sync
        error_str = "; ".join(errors[-10:]) if errors else None
        db.log_sync(
            folder_id=folder['id'],
            files_synced=len(files),
            files_processed=total_processed,
            errors=error_str,
            synced_by=sync_user_id
        )

        logger.info(f"Webhook sync complete: {total_processed}/{len(files)} files processed")

    except Exception as e:
        logger.error(f"Error in sync_changed_files: {e}", exc_info=True)
