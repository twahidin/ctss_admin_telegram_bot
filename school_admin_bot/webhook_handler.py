"""
Google Drive Webhook Handler
Handles incoming webhook notifications from Google Drive.

Auto-sync: When a Google Doc, Sheet, Presentation, PDF, or image is modified or
uploaded in the watched root folder (or any subfolder), the webhook triggers and
we sync only those file types. Run /registerwebhook once to enable (requires
WEBHOOK_URL and a public URL). Webhook watches the entire folder tree.
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
            logger.info(f"✅ Webhook verification challenge received: {challenge[:20]}...")
            return challenge, 200
        # Health check endpoint
        return jsonify({
            "status": "ok",
            "service": "google_drive_webhook",
            "timestamp": datetime.now().isoformat()
        }), 200

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
                logger.info(f"✅ Webhook sync notification received (channel: {channel_id[:16] if channel_id else 'unknown'}...)")
                return "OK", 200

            elif resource_state == 'update' or resource_state == 'change':
                # File changed - trigger sync
                logger.info(f"📥 File change detected via webhook (channel: {channel_id[:16] if channel_id else 'unknown'}...), triggering sync...")
                
                # Process changes asynchronously (don't block webhook response)
                if bot_instance and drive_sync_instance:
                    # Schedule sync in background
                    import threading
                    thread = threading.Thread(
                        target=process_drive_changes_with_notification,
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


def notify_admins(message):
    """Send notification to superadmins"""
    if bot_instance:
        try:
            from config import SUPER_ADMIN_IDS
            import asyncio
            
            async def send_notifications():
                for admin_id in SUPER_ADMIN_IDS[:1]:  # Notify first admin only
                    try:
                        await bot_instance.app.bot.send_message(
                            admin_id,
                            message,
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.debug(f"Could not send notification to {admin_id}: {e}")
            
            # Try to get existing event loop or create new one
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is running, schedule the coroutine
                    asyncio.create_task(send_notifications())
                else:
                    loop.run_until_complete(send_notifications())
            except RuntimeError:
                # No event loop, create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_notifications())
                loop.close()
        except Exception as e:
            logger.debug(f"Error in notify_admins: {e}")


def process_drive_changes_with_notification(channel_id):
    """Process drive changes and send notifications"""
    # Send initial notification
    notify_admins(
        "🔄 *Webhook Activity*\n\n"
        "Google Drive change detected!\n"
        "Syncing files automatically..."
    )
    
    # Process changes
    process_drive_changes(channel_id)


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
                
                # Skip folders - we only sync files
                if mime_type == 'application/vnd.google-apps.folder':
                    logger.debug(f"Skipping folder: {file_name}")
                    continue
                
                # Check if file is in our watched folder or any subfolder
                parents = file_info.get('parents', [])
                
                # Only auto-sync Google Docs, Sheets, PDFs, and images (skip other types)
                AUTO_SYNC_MIME_TYPES = (
                    'application/vnd.google-apps.document',
                    'application/vnd.google-apps.spreadsheet',
                    'application/vnd.google-apps.presentation',
                    'application/pdf',
                    'application/vnd.google-apps.shortcut',
                )
                is_doc_or_sheet = mime_type in AUTO_SYNC_MIME_TYPES
                is_pdf = mime_type == 'application/pdf' or (file_name and file_name.lower().endswith('.pdf'))
                is_image = (mime_type or '').startswith('image/')
                if not (is_doc_or_sheet or is_pdf or is_image):
                    logger.debug(f"Skipping auto-sync for unsupported type: {file_name} ({mime_type})")
                    continue

                # Check if file is directly in watched folder
                if folder_id in parents or file_id == folder_id:
                    files_to_sync.append({
                        'id': file_id,
                        'name': file_name,
                        'mimeType': mime_type,
                        'parents': parents
                    })
                else:
                    # Check if file is in a subfolder of watched folder (recursive)
                    # We need to check if any parent is a subfolder of the watched folder
                    if parents:
                        # Get the immediate parent
                        parent_id = parents[0]
                        try:
                            # Check if this parent is in the watched folder tree
                            parent_info = drive_sync_instance.service.files().get(
                                fileId=parent_id,
                                fields='id, name, parents, mimeType'
                            ).execute()
                            
                            # Walk up the tree to see if we reach the watched folder
                            current_parent = parent_info
                            max_depth = 10
                            depth = 0
                            in_watched_tree = False
                            
                            while current_parent and depth < max_depth:
                                parent_parents = current_parent.get('parents', [])
                                if folder_id in parent_parents:
                                    # This is a subfolder of watched folder
                                    in_watched_tree = True
                                    break
                                if not parent_parents:
                                    break
                                # Move up one level
                                current_parent = drive_sync_instance.service.files().get(
                                    fileId=parent_parents[0],
                                    fields='id, name, parents, mimeType'
                                ).execute()
                                depth += 1
                            
                            if in_watched_tree:
                                files_to_sync.append({
                                    'id': file_id,
                                    'name': file_name,
                                    'mimeType': mime_type,
                                    'parents': parents,
                                    '_in_subfolder': True
                                })
                        except Exception as e:
                            logger.debug(f"Error checking subfolder for {file_name}: {e}")
                else:
                    # Check if this is a tracked shortcut target file
                    shortcut_info = db.get_shortcut_by_target(file_id)
                    if shortcut_info and shortcut_info['watched_folder_id'] == folder_id:
                        logger.info(f"Detected change to shortcut target: {file_name} (target of {shortcut_info['shortcut_name']})")
                        files_to_sync.append({
                            'id': file_id,
                            'name': file_name,
                            'mimeType': mime_type,
                            '_is_shortcut_target': True,
                            '_shortcut_name': shortcut_info['shortcut_name']
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
                # Track shortcut targets for watching
                if file.get('mimeType') == 'application/vnd.google-apps.shortcut':
                    shortcut_info = file.get('_shortcut_info')
                    if shortcut_info:
                        db.save_shortcut_target(
                            shortcut_id=file['id'],
                            shortcut_name=file['name'],
                            target_file_id=shortcut_info['target_id'],
                            target_file_name=shortcut_info.get('target_name', ''),
                            watched_folder_id=folder['drive_folder_id']
                        )
                        logger.info(f"Tracking shortcut target: {shortcut_info['target_id']} for shortcut {file['name']}")
                
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

                # Determine which folder this file belongs to (for display and access control)
                file_folder_name = folder['folder_name']
                effective_folder = folder  # Use for drive_folder_id and DB updates
                if file.get('_in_subfolder') and file.get('parents'):
                    parent_id = file['parents'][0]
                    try:
                        parent_info = drive_sync_instance.service.files().get(
                            fileId=parent_id,
                            fields='name'
                        ).execute()
                        subfolder_name = parent_info.get('name', 'Unknown')
                        file_folder_name = f"{folder['folder_name']}/{subfolder_name}"
                        # Use subfolder's drive_folder_id for access control if it's in our DB
                        subfolder_db = db.get_folder_by_drive_id(parent_id)
                        if subfolder_db:
                            effective_folder = subfolder_db
                    except Exception as e:
                        logger.debug(f"Could not resolve subfolder for file {file.get('name')}: {e}")
                
                # Save to database (use effective_folder so access control works per subfolder).
                # Include drive_file_id for upsert: one entry per file per day (update on re-sync).
                content_data = {
                    "type": file_type,
                    "file_name": file['name'],
                    "extracted_text": extracted_text,
                    "source": "google_drive_webhook",
                    "folder": file_folder_name,
                    "drive_folder_id": effective_folder['drive_folder_id'],  # For access control
                    "drive_file_id": file['id'],  # For upsert: same file re-synced today = update row
                }

                db.add_or_update_drive_entry(sync_user_id, category, content_data)
                total_processed += 1

            except Exception as e:
                logger.error(f"Error processing file {file.get('name', 'unknown')}: {e}")
                errors.append(f"{file.get('name', 'unknown')}: {str(e)}")

        # Update sync time for the webhook's folder (root)
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

        logger.info(f"✅ Webhook auto-sync complete: {total_processed}/{len(files)} files (Docs, Sheets, PDFs, images)")
        
        # Notify admins of successful sync
        notify_admins(
            f"✅ *Auto-Sync Complete*\n\n"
            f"*Files processed:* {total_processed}/{len(files)}\n"
            f"*Folder:* {folder['folder_name']}\n"
            f"*Time:* {datetime.now().strftime('%H:%M:%S')}"
        )

    except Exception as e:
        logger.error(f"Error in sync_changed_files: {e}", exc_info=True)
