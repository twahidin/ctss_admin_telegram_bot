import os
import io
import re
import json
import base64
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# Singapore timezone for "today" context in prompts
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


def get_singapore_now():
    """Return current datetime in Singapore timezone."""
    return datetime.now(SINGAPORE_TZ)


def get_singapore_date_time_str():
    """Return human-readable Singapore date and time for prompts (e.g. '28 January 2026, 5:20 PM SGT')."""
    now = get_singapore_now()
    return now.strftime("%d %B %Y, %I:%M %p SGT")
import fitz  # PyMuPDF for PDF processing
from psycopg.rows import dict_row
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import anthropic
from database import Database
from drive_sync import DriveSync
from config import (
    TELEGRAM_TOKEN,
    CLAUDE_API_KEY,
    TAGS,
    SUPER_ADMIN_IDS,
    DAILY_CODE_LENGTH,
    PERIOD_TIMES,
    REMINDER_MINUTES_BEFORE,
    GOOGLE_DRIVE_ROOT_FOLDER_ID,
    SYNC_SCHEDULE,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
SELECTING_TAG, AWAITING_CONTENT, AWAITING_DETAILS, AWAITING_CODE = range(4)
AWAITING_USER_ID, AWAITING_ROLE = range(4, 6)
UPLOAD_MENU, PRIVACY_WARNING, SELECTING_UPLOAD_TO_DELETE = range(6, 9)
RELIEF_ACTIVATION, SELECTING_RELIEF_REMINDERS = range(9, 11)

# Initialize database
db = Database()

# Initialize Claude client
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


class SchoolAdminBot:
    def __init__(self):
        self.app = None
        # Initialize Drive sync (optional, only if configured)
        self.drive_sync = None
        try:
            if GOOGLE_DRIVE_ROOT_FOLDER_ID:
                self.drive_sync = DriveSync()
                logger.info("Google Drive sync initialized")
        except Exception as e:
            logger.warning(f"Google Drive sync not available: {e}")

    def analyze_image(self, image_data: bytes, category: str) -> str:
        """Analyze image using Claude's vision API and extract text/information"""
        try:
            # Convert image to base64
            base64_image = base64.b64encode(image_data).decode('utf-8')
            
            # Determine media type (assume JPEG/PNG for photos)
            media_type = "image/png"
            if image_data[:2] == b'\xff\xd8':
                media_type = "image/jpeg"
            
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                timeout=30.0,  # 30 second timeout
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64_image,
                                },
                            },
                            {
                                "type": "text",
                                "text": f"""Extract ALL text from this "{category}" image. Include names, classes, times, rooms. Be concise."""
                            }
                        ],
                    }
                ],
            )
            
            extracted_text = response.content[0].text
            logger.info(f"Extracted text from image: {extracted_text[:200]}...")
            return extracted_text
            
        except Exception as e:
            logger.error(f"Image analysis error: {e}")
            return f"[Image analysis failed: {str(e)}]"

    def analyze_pdf(self, pdf_data: bytes, category: str) -> str:
        """Analyze PDF - first try direct text extraction, fall back to image analysis"""
        try:
            # Open PDF from bytes
            pdf_document = fitz.open(stream=pdf_data, filetype="pdf")
            all_extracted_text = []
            
            # Limit to first 5 pages to avoid timeout
            max_pages = min(len(pdf_document), 5)
            
            # First, try to extract text directly (much faster)
            has_text = False
            for page_num in range(max_pages):
                page = pdf_document[page_num]
                text = page.get_text().strip()
                if text:
                    has_text = True
                    all_extracted_text.append(f"--- Page {page_num + 1} ---\n{text}")
            
            # If we got text directly, use it
            if has_text and len("\n".join(all_extracted_text)) > 100:
                pdf_document.close()
                combined_text = "\n\n".join(all_extracted_text)
                logger.info(f"Extracted text directly from PDF ({max_pages} pages): {combined_text[:200]}...")
                return combined_text
            
            # Otherwise, fall back to image analysis (for scanned PDFs)
            # Only analyze first 2 pages to avoid timeout
            all_extracted_text = []
            max_pages_for_ocr = min(len(pdf_document), 2)
            
            for page_num in range(max_pages_for_ocr):
                page = pdf_document[page_num]
                
                # Convert page to image (lower resolution to speed up)
                mat = fitz.Matrix(1.5, 1.5)  # 1.5x zoom (reduced from 2x)
                pix = page.get_pixmap(matrix=mat)
                
                # Convert to PNG bytes
                img_bytes = pix.tobytes("png")
                
                # Analyze this page image
                page_text = self.analyze_image(img_bytes, category)
                all_extracted_text.append(f"--- Page {page_num + 1} ---\n{page_text}")
            
            pdf_document.close()
            
            combined_text = "\n\n".join(all_extracted_text)
            logger.info(f"Extracted text from PDF via OCR ({max_pages_for_ocr} pages): {combined_text[:200]}...")
            return combined_text
            
        except Exception as e:
            logger.error(f"PDF analysis error: {e}")
            return f"[PDF analysis failed: {str(e)}]"

    def parse_relief_data(self, extracted_text: str) -> list:
        """
        Parse relief information from extracted text using Claude.
        Returns a list of relief entries with teacher names and periods.
        """
        try:
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                timeout=30.0,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Extract ALL relief teacher assignments from this text. 
For each relief assignment, extract:
- relief_teacher: Name of the teacher doing the relief
- original_teacher: Name of the absent teacher (if mentioned)
- period: The period number (0-25)
- class: The class name (e.g., "3A", "4E1")
- room: The classroom/venue (if mentioned)

Return ONLY valid JSON array. No explanation.
Format: [{{"relief_teacher": "Name", "original_teacher": "Name or null", "period": "5", "class": "3A", "room": "Room"}}]

If no relief data found, return empty array: []

Text to parse:
{extracted_text}"""
                    }
                ],
            )
            
            result_text = response.content[0].text.strip()
            
            # Try to extract JSON array from the response
            # Handle cases where Claude might wrap it in markdown
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()
            
            relief_data = json.loads(result_text)
            logger.info(f"Parsed relief data: {relief_data}")
            return relief_data
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse relief JSON: {e}, response: {result_text[:500]}")
            return []
        except Exception as e:
            logger.error(f"Relief parsing error: {e}")
            return []

    def match_teacher_to_user(self, teacher_name: str) -> int | None:
        """
        Try to match a teacher name to a registered user.
        Returns telegram_id if found, None otherwise.
        """
        if not teacher_name:
            return None
            
        # Clean the name
        clean_name = teacher_name.strip()
        
        # Try exact match first
        user = db.find_user_by_name(clean_name)
        if user:
            return user["telegram_id"]
        
        # Try matching partial names (last name or first name)
        all_users = db.get_all_users()
        name_lower = clean_name.lower()
        
        for user in all_users:
            display_name = user["display_name"].lower()
            # Check if the teacher name contains the user's display name or vice versa
            if name_lower in display_name or display_name in name_lower:
                return user["telegram_id"]
            # Check individual parts of the name
            name_parts = name_lower.split()
            display_parts = display_name.split()
            for part in name_parts:
                if len(part) > 2 and part in display_parts:
                    return user["telegram_id"]
        
        return None

    def get_period_start_time(self, period: str) -> time | None:
        """Get the start time for a given period number."""
        time_str = PERIOD_TIMES.get(str(period))
        if time_str:
            hour, minute = map(int, time_str.split(":"))
            return time(hour=hour, minute=minute)
        return None

    def calculate_reminder_time(self, period_time: time) -> time:
        """Calculate when to send the reminder (X minutes before period)."""
        today = datetime.today()
        period_dt = datetime.combine(today, period_time)
        reminder_dt = period_dt - timedelta(minutes=REMINDER_MINUTES_BEFORE)
        return reminder_dt.time()

    async def process_relief_reminders(self, relief_data: list, created_by: int) -> list:
        """
        Process parsed relief data and create reminder entries.
        Returns list of created reminders with match status.
        """
        created_reminders = []
        
        for entry in relief_data:
            teacher_name = entry.get("relief_teacher", "").strip()
            if not teacher_name:
                continue
                
            # Match teacher to user
            telegram_id = self.match_teacher_to_user(teacher_name)
            
            # Get period time
            period = entry.get("period", "")
            period_time = self.get_period_start_time(period)
            
            if not period_time:
                logger.warning(f"Could not find time for period {period}")
                continue
            
            # Calculate reminder time
            reminder_time = self.calculate_reminder_time(period_time)
            
            # Create reminder entry
            reminder_id = db.add_relief_reminder(
                teacher_name=teacher_name,
                teacher_telegram_id=telegram_id,
                relief_time=reminder_time,
                period=str(period),
                class_info=entry.get("class", ""),
                room=entry.get("room", ""),
                original_teacher=entry.get("original_teacher", ""),
                created_by=created_by,
                activated=False
            )
            
            created_reminders.append({
                "id": reminder_id,
                "teacher_name": teacher_name,
                "telegram_id": telegram_id,
                "matched": telegram_id is not None,
                "period": period,
                "period_time": PERIOD_TIMES.get(str(period), ""),
                "reminder_time": reminder_time.strftime("%H:%M"),
                "class": entry.get("class", ""),
                "room": entry.get("room", ""),
            })
        
        return created_reminders

    async def send_relief_reminder(self, context: ContextTypes.DEFAULT_TYPE, reminder: dict):
        """Send a relief reminder notification to a teacher."""
        telegram_id = reminder.get("teacher_telegram_id")
        if not telegram_id:
            return False
            
        try:
            period = reminder.get("period", "?")
            class_info = reminder.get("class_info", "")
            room = reminder.get("room", "")
            original = reminder.get("original_teacher", "")
            relief_time = reminder.get("relief_time", "")
            
            message = (
                f"⏰ *Relief Reminder!*\n\n"
                f"📚 Period {period} ({relief_time})\n"
            )
            
            if class_info:
                message += f"🎓 Class: {class_info}\n"
            if room:
                message += f"🚪 Room: {room}\n"
            if original:
                message += f"👤 Covering for: {original}\n"
            
            message += f"\n_Starting in {REMINDER_MINUTES_BEFORE} minutes_"
            
            await context.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="Markdown"
            )
            
            # Mark as sent
            db.mark_reminder_sent(reminder["id"])
            logger.info(f"Sent relief reminder to {telegram_id} for period {period}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send relief reminder: {e}")
            return False

    async def relief_reminder_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Job that runs every minute to check and send due relief reminders."""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        
        # Get pending reminders that are due
        pending = db.get_pending_relief_reminders(current_time)
        
        for reminder in pending:
            await self.send_relief_reminder(context, reminder)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command - register user"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name

        # Check if user exists
        user = db.get_user(user_id)

        if user:
            role = user["role"]
            await update.message.reply_text(
                f"Welcome back, {username}!\n"
                f"Your role: {role.upper()}\n\n"
                f"Use /help to see available commands."
            )
        else:
            # Auto-register as viewer if super admin, otherwise needs to be added
            if user_id in SUPER_ADMIN_IDS:
                db.add_user(user_id, username, "superadmin", user_id)
                await update.message.reply_text(
                    f"Welcome, Super Admin!\n\n"
                    f"You have full access. Use /help for commands."
                )
            else:
                await update.message.reply_text(
                    f"Hello {username}!\n\n"
                    f"You need to be added by an admin to use this bot.\n"
                    f"Your Telegram ID: `{user_id}`\n\n"
                    f"Please share this ID with an admin.",
                    parse_mode="Markdown",
                )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show basic help for all users"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user:
            await update.message.reply_text(
                "You're not registered. Use /start to begin."
            )
            return

        role = user["role"]

        help_text = "📚 *HELP - GENERAL COMMANDS*\n\n"
        help_text += "*Information & Queries:*\n"
        help_text += "/ask [question] - Ask questions about today's information\n"
        help_text += "  • Example: /ask Who's teaching 3A at 10am?\n"
        help_text += "/today - View all categories and entry counts for today\n\n"

        # Show role-specific help links
        if role == "student_admin":
            help_text += "*Student Movement Admin:*\n"
            help_text += "/upload - Upload Student Movement information\n"
            help_text += "  • Use Remove menu to clear all Student Movement for today\n"
        elif role == "relief_member":
            help_text += "*Additional Help:*\n"
            help_text += "/helprelief - Relief member specific commands\n"
        elif role in ["admin", "superadmin"]:
            help_text += "*Additional Help:*\n"
            help_text += "/helprelief - Relief management commands\n"
            help_text += "/helpadmin - Admin and management commands\n"

        help_text += "\n/help - Show this help message"

        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def helprelief(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show relief member help"""
        try:
            user_id = update.effective_user.id
            user = db.get_user(user_id)

            if not user:
                await update.message.reply_text(
                    "You're not registered. Use /start to begin."
                )
                return

            # Show help to all users, but indicate if they don't have access
            has_access = user["role"] in ["relief_member", "admin", "superadmin"]
            
            if not has_access:
                help_text = "🔄 *RELIEF HELP - RELIEF MANAGEMENT*\n\n"
                help_text += "❌ *You don't have access to these commands.*\n\n"
                help_text += "These commands are available to:\n"
                help_text += "• relief\\_member\n"
                help_text += "• admin\n"
                help_text += "• superadmin\n\n"
                help_text += "Use /help to see commands available to your role."
                await update.message.reply_text(help_text, parse_mode="Markdown")
                return

            help_text = "🔄 *RELIEF HELP - RELIEF MANAGEMENT*\n\n"
            help_text += "*Relief Reminders:*\n"
            help_text += "/reliefstatus - View all relief reminders for today\n"
            help_text += "  • Shows active reminders and their status\n"
            help_text += "/cancelrelief - Cancel all relief reminders for today\n"
            help_text += "  • Use this if relief assignments are cancelled\n\n"
            
            help_text += "*Google Drive Access:*\n"
            help_text += "/sync - Sync files from Google Drive folders\n"
            help_text += "  • Downloads and processes files from accessible folders\n"
            help_text += "/syncstatus - View sync history for today\n\n"
            
            help_text += "*General Commands:*\n"
            help_text += "/help - View general help commands\n"
            
            if user["role"] in ["admin", "superadmin"]:
                help_text += "/helpadmin - View admin management commands\n"

            try:
                await update.message.reply_text(help_text, parse_mode="Markdown")
            except Exception as parse_error:
                # Fallback to plain text if Markdown fails
                logger.warning(f"Markdown parse error in helprelief, using plain text: {parse_error}")
                # Remove all Markdown formatting
                help_text_plain = help_text.replace("*", "").replace("_", "").replace("\\", "")
                await update.message.reply_text(help_text_plain)
        except Exception as e:
            logger.error(f"Error in helprelief: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def helpadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show admin help - for admins and superadmins"""
        try:
            user_id = update.effective_user.id
            user = db.get_user(user_id)

            if not user:
                await update.message.reply_text(
                    "You're not registered. Use /start to begin."
                )
                return

            # Show help to all users, but indicate if they don't have access
            has_access = user["role"] in ["admin", "superadmin"]
            
            if not has_access:
                help_text = "🔧 *ADMIN HELP - MANAGEMENT COMMANDS*\n\n"
                help_text += "❌ *You don't have access to these commands.*\n\n"
                help_text += "These commands are available to:\n"
                help_text += "• admin\n"
                help_text += "• superadmin\n\n"
                help_text += "Use /help to see commands available to your role."
                await update.message.reply_text(help_text, parse_mode="Markdown")
                return

            help_text = "🔧 *ADMIN HELP - MANAGEMENT COMMANDS*\n\n"
            
            help_text += "*File Upload:*\n"
            help_text += "/upload - Upload new information or remove existing uploads\n"
            help_text += "  • Supports photos, PDFs, and text\n"
            help_text += "/myuploads - View your uploads for today\n\n"
            
            help_text += "*User Management:*\n"
            help_text += "/add [user_id] [name] - Add a new user to the system\n"
            help_text += "  • Example: /add 123456789 John Teacher\n"
            help_text += "/remove [user_id] - Remove a user from the system\n"
            help_text += "/promote [user_id] [role] - Change a user's role\n"
            help_text += "  • Roles: viewer, relief\\_member, admin, student\\_admin\n"
            help_text += "  • Example: /promote 123456789 relief\\_member\n"
            help_text += "/list - Show all registered users and their roles\n\n"
            
            help_text += "*Google Drive Management:*\n"
            help_text += "/sync - Sync files from accessible Google Drive folders\n"
            help_text += "/listfolders - View all folders and their access configuration\n"
            help_text += "/drivefolder - View connected Google Drive folder information\n"
            help_text += "/syncstatus - View sync history and status\n"
            
            if user["role"] == "superadmin":
                help_text += "/setfolder Folder Name roles - Configure folder access\n"
                help_text += "  • Example: /setfolder Relief Timetable admin,relief\\_member\n\n"
            
            help_text += "*Relief Management:*\n"
            help_text += "/reliefstatus - View today's relief reminders\n"
            help_text += "/cancelrelief - Cancel all relief reminders\n\n"
            
            help_text += "*Other Commands:*\n"
            help_text += "/help - View general help commands\n"
            help_text += "/helprelief - View relief management commands\n"
            
            if user["role"] == "superadmin":
                help_text += "/helpsuper - View super admin commands\n"

            try:
                await update.message.reply_text(help_text, parse_mode="Markdown")
            except Exception as parse_error:
                # Fallback to plain text if Markdown fails
                logger.warning(f"Markdown parse error in helpadmin, using plain text: {parse_error}")
                # Remove all Markdown formatting
                help_text_plain = help_text.replace("*", "").replace("_", "").replace("\\", "")
                await update.message.reply_text(help_text_plain)
        except Exception as e:
            logger.error(f"Error in helpadmin: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def helpsuper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show super admin help"""
        try:
            user_id = update.effective_user.id
            user = db.get_user(user_id)
            
            if not user:
                await update.message.reply_text(
                    "You're not registered. Use /start to begin."
                )
                return
            
            # Check if user is a protected superadmin from config
            is_protected_superadmin = user_id in SUPER_ADMIN_IDS
            is_superadmin_role = user["role"] == "superadmin"
            
            if not (is_protected_superadmin or is_superadmin_role):
                help_text = "👑 *SUPER ADMIN HELP - SYSTEM COMMANDS*\n\n"
                help_text += "❌ *You don't have access to these commands.*\n\n"
                help_text += "These commands are available to:\n"
                help_text += "• Protected super admins (from config)\n"
                help_text += "• Users with superadmin role\n\n"
                help_text += "Use /help to see commands available to your role."
                await update.message.reply_text(help_text, parse_mode="Markdown")
                return

            help_text = "👑 *SUPER ADMIN HELP - SYSTEM COMMANDS*\n\n"
            help_text += "*User Management:*\n"
            help_text += "/massupload - Upload CSV file to replace all users\n"
            help_text += "  • CSV format: telegram_id,name,role\n"
            help_text += "  • Roles: viewer, relief\\_member, admin, student\\_admin\n"
            help_text += "  • This replaces all users except protected super admins\n"
            help_text += "/addsuperadmin [user_id] - Add a new super admin\n"
            help_text += "/removesuperadmin [user_id] - Remove a super admin\n"
            help_text += "/listsuperadmins - List all super admins\n\n"
            help_text += "*Google Drive Configuration:*\n"
            help_text += "/setfolder Folder Name roles - Configure folder access\n"
            help_text += "  • Example: /setfolder Relief Timetable admin,relief\\_member\n"
            help_text += "  • Available roles: viewer, relief\\_member, admin, superadmin\n"
            help_text += "/listfolders - View all folders and their access configuration\n"
            help_text += "  • Folders sync on schedule (Relief Committee 6pm, others 7:45am)\n\n"
            help_text += "*Testing & Debugging:*\n"
            help_text += "/assume [role] - Assume a different role for testing\n"
            help_text += "  • Roles: viewer, relief\\_member, admin, student\\_admin\n"
            help_text += "  • Example: /assume viewer\n"
            help_text += "/resume - Resume your original superadmin role\n\n"
            help_text += "*System Management:*\n"
            help_text += "/stats - Show bot usage statistics\n"
            help_text += "/purge - Manually trigger data purge (usually runs at 11 PM)\n\n"
            help_text += "*Other Commands:*\n"
            help_text += "/help - View general help commands\n"
            help_text += "/helprelief - View relief management commands\n"
            help_text += "/helpadmin - View admin management commands\n\n"
            help_text += f"Your account (ID: {user_id}) is protected and cannot be removed."

            try:
                await update.message.reply_text(help_text, parse_mode="Markdown")
            except Exception as parse_error:
                # Fallback to plain text if Markdown fails
                logger.warning(f"Markdown parse error in helpsuper, using plain text: {parse_error}")
                # Remove all Markdown formatting
                help_text_plain = help_text.replace("*", "").replace("_", "").replace("\\", "")
                await update.message.reply_text(help_text_plain)
        except Exception as e:
            logger.error(f"Error in helpsuper: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def upload_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start upload process - show initial menu"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin", "student_admin"]:
            await update.message.reply_text("❌ You don't have upload permissions.")
            return ConversationHandler.END

        # Check user's uploads count today (student_admin sees Student Movement remove option)
        is_student_admin = user["role"] == "student_admin"
        user_uploads = db.get_user_uploads_today(user_id)
        upload_count = len(user_uploads)

        # Build menu buttons
        buttons = [
            [InlineKeyboardButton("📤 Upload New Information", callback_data="upload_new")],
        ]
        
        # Remove options: student_admin gets remove one + remove all; others get per-upload remove
        if is_student_admin:
            sm_entries = db.get_student_movement_entries_today()
            if sm_entries:
                buttons.append([InlineKeyboardButton(f"🗑️ Remove One Student Movement ({len(sm_entries)} total)", callback_data="upload_remove_one_sm")])
            buttons.append([InlineKeyboardButton("🗑️ Remove All Student Movement", callback_data="upload_remove_student_movement")])
        elif upload_count > 0:
            buttons.append([InlineKeyboardButton(f"🗑️ Remove One Upload ({upload_count} total)", callback_data="upload_remove_one")])
            buttons.append([InlineKeyboardButton("🗑️ Remove All My Uploads", callback_data="upload_remove_all")])
        
        buttons.append([InlineKeyboardButton("❌ Exit", callback_data="upload_exit")])
        
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(
            "📤 *UPLOAD MENU*\n\n"
            f"You have *{upload_count}* upload(s) today.\n\n"
            "What would you like to do?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        return UPLOAD_MENU

    async def handle_upload_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle upload menu selection"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        choice = query.data
        
        logger.info(f"Upload menu callback received: {choice} from user {user_id}")
        
        if choice == "upload_exit":
            await query.edit_message_text("👋 Upload cancelled. Come back anytime!")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice == "upload_new":
            # Show privacy warning before proceeding
            buttons = [
                [InlineKeyboardButton("✅ I Agree - Continue", callback_data="privacy_agree")],
                [InlineKeyboardButton("❌ Cancel", callback_data="privacy_cancel")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            
            await query.edit_message_text(
                "⚠️ *IMPORTANT NOTICE*\n\n"
                "Before uploading, please be aware:\n\n"
                "🔒 *DO NOT* upload sensitive or confidential information such as:\n"
                "• Personal NRIC/IC numbers\n"
                "• Home addresses\n"
                "• Medical information\n"
                "• Financial details\n"
                "• Private phone numbers\n\n"
                "📅 *All uploaded data will be automatically deleted after ONE DAY.*\n\n"
                "By clicking 'I Agree', you confirm that your upload does not contain sensitive or confidential information.\n\n"
                "Do you agree to proceed?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return PRIVACY_WARNING
        
        elif choice == "upload_remove_one_sm":
            # student_admin: show list of Student Movement entries to remove one
            sm_entries = db.get_student_movement_entries_today()
            if not sm_entries:
                await query.edit_message_text("📭 No Student Movement entries to remove.")
                context.user_data.clear()
                return ConversationHandler.END
            
            context.user_data["delete_mode"] = "student_movement"
            buttons = []
            for entry in sm_entries:
                entry_id = entry["id"]
                tag = entry["tag"]
                timestamp = entry["timestamp"]
                content = entry.get("content", {})
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except:
                        content = {}
                content_type = content.get("type", "document") if isinstance(content, dict) else "document"
                label = f"🗑️ [{tag}] {content_type} @ {timestamp}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"delete_entry_{entry_id}")])
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="delete_cancel")])
            keyboard = InlineKeyboardMarkup(buttons)
            await query.edit_message_text(
                "🗑️ *SELECT STUDENT MOVEMENT ENTRY TO REMOVE*\n\n"
                "Choose which entry to delete:",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return SELECTING_UPLOAD_TO_DELETE
        
        elif choice == "upload_remove_student_movement":
            # student_admin only: remove all Student Movement entries
            buttons = [
                [InlineKeyboardButton("✅ Yes, Remove All Student Movement", callback_data="confirm_remove_student_movement")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_remove_all")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            await query.edit_message_text(
                "⚠️ *CONFIRM DELETION*\n\n"
                "Are you sure you want to remove *ALL* Student Movement information for today?\n\n"
                "This action cannot be undone.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return UPLOAD_MENU
        
        elif choice == "confirm_remove_student_movement":
            deleted_count = db.delete_student_movement_entries_today()
            await query.edit_message_text(f"✅ Removed *{deleted_count}* Student Movement entry/entries.", parse_mode="Markdown")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice == "upload_remove_one":
            # Show list of user's uploads to select from
            user_uploads = db.get_user_uploads_today(user_id)
            
            if not user_uploads:
                await query.edit_message_text("📭 You have no uploads to remove.")
                context.user_data.clear()
                return ConversationHandler.END
            
            buttons = []
            for entry in user_uploads:
                entry_id = entry["id"]
                tag = entry["tag"]
                timestamp = entry["timestamp"]
                content_type = entry["content"]["type"]
                
                label = f"🗑️ [{tag}] {content_type} @ {timestamp}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"delete_entry_{entry_id}")])
            
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="delete_cancel")])
            
            keyboard = InlineKeyboardMarkup(buttons)
            
            await query.edit_message_text(
                "🗑️ *SELECT UPLOAD TO REMOVE*\n\n"
                "Choose which upload to delete:",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return SELECTING_UPLOAD_TO_DELETE
        
        elif choice == "upload_remove_all":
            # Confirm removal of all uploads
            buttons = [
                [InlineKeyboardButton("✅ Yes, Remove All", callback_data="confirm_remove_all")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_remove_all")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            
            user_uploads = db.get_user_uploads_today(user_id)
            count = len(user_uploads)
            
            await query.edit_message_text(
                f"⚠️ *CONFIRM DELETION*\n\n"
                f"Are you sure you want to remove *ALL {count}* of your uploads from today?\n\n"
                f"This action cannot be undone.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return UPLOAD_MENU
        
        elif choice == "confirm_remove_all":
            deleted_count = db.delete_all_user_uploads_today(user_id)
            await query.edit_message_text(f"✅ Removed *{deleted_count}* upload(s).", parse_mode="Markdown")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice == "cancel_remove_all":
            await query.edit_message_text("👍 Deletion cancelled.")
            context.user_data.clear()
            return ConversationHandler.END
        
        return UPLOAD_MENU

    async def handle_privacy_warning(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle privacy warning response"""
        query = update.callback_query
        await query.answer()
        
        choice = query.data
        
        if choice == "privacy_cancel":
            await query.edit_message_text("👋 Upload cancelled. No information was uploaded.")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice == "privacy_agree":
            # Proceed to tag selection (student_admin only sees STUDENT_MOVEMENT)
            user = db.get_user(query.from_user.id)
            is_student_admin = user and user.get("role") == "student_admin"
            if is_student_admin:
                tags_for_user = ["STUDENT_MOVEMENT"]
            else:
                tags_for_user = TAGS
            tag_buttons = [[f"{i+1}️⃣ {tag}"] for i, tag in enumerate(tags_for_user)]
            tag_buttons.append(["❌ Cancel"])
            reply_markup = ReplyKeyboardMarkup(tag_buttons, one_time_keyboard=True)
            
            await query.edit_message_text("✅ Thank you for agreeing. Proceeding to category selection...")
            
            await query.message.reply_text(
                "📤 *SELECT CATEGORY*\n\n"
                "Choose a category for your upload:",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return SELECTING_TAG
        
        return PRIVACY_WARNING

    async def handle_delete_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle selection of entry to delete"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        choice = query.data
        
        if choice == "delete_cancel":
            await query.edit_message_text("👍 Deletion cancelled.")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice.startswith("delete_entry_"):
            entry_id = int(choice.replace("delete_entry_", ""))
            
            # Confirm before deleting
            context.user_data["pending_delete_id"] = entry_id
            
            buttons = [
                [InlineKeyboardButton("✅ Yes, Delete", callback_data="confirm_delete_single")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete_single")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            
            await query.edit_message_text(
                "⚠️ *CONFIRM DELETION*\n\n"
                "Are you sure you want to delete this upload?\n\n"
                "This action cannot be undone.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return SELECTING_UPLOAD_TO_DELETE
        
        elif choice == "confirm_delete_single":
            entry_id = context.user_data.get("pending_delete_id")
            if entry_id:
                is_student_movement_delete = context.user_data.get("delete_mode") == "student_movement"
                if is_student_movement_delete:
                    deleted = db.delete_student_movement_entry_by_id(entry_id)
                else:
                    deleted = db.delete_entry_by_id(entry_id, user_id)
                if deleted:
                    await query.edit_message_text("✅ Entry deleted successfully.")
                else:
                    await query.edit_message_text("❌ Could not delete entry. It may have already been removed.")
            context.user_data.clear()
            return ConversationHandler.END
        
        elif choice == "cancel_delete_single":
            await query.edit_message_text("👍 Deletion cancelled.")
            context.user_data.clear()
            return ConversationHandler.END
        
        return SELECTING_UPLOAD_TO_DELETE

    async def tag_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tag selection"""
        text = update.message.text
        
        # Check if user wants to cancel
        if text == "❌ Cancel":
            await update.message.reply_text(
                "👋 Upload cancelled.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data.clear()
            return ConversationHandler.END
        
        try:
            user = db.get_user(update.effective_user.id)
            is_student_admin = user and user.get("role") == "student_admin"
            tags_for_user = ["STUDENT_MOVEMENT"] if is_student_admin else TAGS
            tag_number = int(text.split("️⃣")[0]) - 1
            if 0 <= tag_number < len(tags_for_user):
                selected_tag = tags_for_user[tag_number]
                context.user_data["selected_tag"] = selected_tag

                await update.message.reply_text(
                    f"Category: *{selected_tag}*\n\n"
                    f"Now send:\n"
                    f"• A photo/image\n"
                    f"• A PDF document\n"
                    f"• Or type your message\n\n"
                    f"Or send /cancel to exit.",
                    parse_mode="Markdown",
                    reply_markup=ReplyKeyboardRemove(),
                )

                return AWAITING_CONTENT
        except (ValueError, IndexError):
            pass

        await update.message.reply_text("❌ Invalid selection. Please choose from the options or Cancel.")
        return SELECTING_TAG

    async def content_received(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle content upload (text, photo, or document) and save directly"""
        user_id = update.effective_user.id
        selected_tag = context.user_data.get("selected_tag")

        # Check if tag was selected
        if not selected_tag:
            await update.message.reply_text(
                "⚠️ No category selected. Please use /upload to start again."
            )
            return ConversationHandler.END

        content_data = {}

        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            caption = update.message.caption or ""

            # Download file
            file = await context.bot.get_file(file_id)
            image_bytes = await file.download_as_bytearray()
            file_path = db.save_file(file_id, "photo", image_bytes)

            # Analyze image with Claude Vision
            await update.message.reply_text("🔍 Analyzing image content...")
            extracted_text = self.analyze_image(bytes(image_bytes), selected_tag)

            content_data = {
                "type": "photo",
                "file_path": file_path,
                "caption": caption,
                "extracted_text": extracted_text,
            }
            content_type = "photo"

        elif update.message.document:
            file_id = update.message.document.file_id
            caption = update.message.caption or ""
            file_name = update.message.document.file_name or ""

            file = await context.bot.get_file(file_id)
            doc_bytes = await file.download_as_bytearray()
            file_path = db.save_file(file_id, "document", doc_bytes)

            extracted_text = ""
            
            # Check if it's a PDF and analyze it
            if file_name.lower().endswith('.pdf') or doc_bytes[:4] == b'%PDF':
                await update.message.reply_text("🔍 Analyzing PDF content... This may take a few seconds.")
                extracted_text = self.analyze_pdf(bytes(doc_bytes), selected_tag)
            # Check if it's an image document
            elif file_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                await update.message.reply_text("🔍 Analyzing image content...")
                extracted_text = self.analyze_image(bytes(doc_bytes), selected_tag)
            # Check if it's a text file
            elif file_name.lower().endswith(('.txt', '.csv', '.text')):
                try:
                    extracted_text = doc_bytes.decode('utf-8')
                    logger.info(f"Read text file: {len(extracted_text)} chars")
                except UnicodeDecodeError:
                    extracted_text = doc_bytes.decode('latin-1')
                    logger.info(f"Read text file (latin-1): {len(extracted_text)} chars")

            content_data = {
                "type": "document",
                "file_path": file_path,
                "caption": caption,
                "file_name": file_name,
                "extracted_text": extracted_text,
            }
            content_type = "document"

        else:  # text
            text_content = update.message.text
            content_data = {"type": "text", "content": text_content}
            content_type = "text"
            extracted_text = text_content

        # student_admin uploads: add folder for Student Movement identification
        if selected_tag == "STUDENT_MOVEMENT":
            user = db.get_user(user_id)
            if user and user.get("role") == "student_admin":
                content_data["folder"] = "Student Movement"
        
        # Save to database
        db.add_entry(user_id, selected_tag, content_data)

        # If this is a RELIEF upload and user is admin/superadmin, offer to set up reminders
        if selected_tag == "RELIEF":
            user = db.get_user(user_id)
            if user and user["role"] in ["admin", "superadmin"]:
                try:
                    await update.message.reply_text("🔍 Parsing relief information for reminders...")
                    
                    # Parse relief data from extracted text
                    logger.info(f"Parsing relief data from text ({len(extracted_text)} chars)")
                    relief_data = self.parse_relief_data(extracted_text)
                    logger.info(f"Parsed relief data: {relief_data}")
                    
                    if relief_data:
                        # Process and create reminder entries
                        logger.info("Processing relief reminders...")
                        created_reminders = await self.process_relief_reminders(relief_data, user_id)
                        logger.info(f"Created {len(created_reminders) if created_reminders else 0} reminders")
                        
                        if created_reminders:
                            # Store reminders in context for activation flow
                            context.user_data["pending_relief_reminders"] = created_reminders
                            
                            # Build summary message
                            matched_count = sum(1 for r in created_reminders if r["matched"])
                            unmatched_count = len(created_reminders) - matched_count
                            
                            summary = f"📋 *Found {len(created_reminders)} relief assignments:*\n\n"
                            
                            for r in created_reminders[:10]:  # Show first 10
                                status = "✅" if r["matched"] else "❓"
                                summary += f"{status} {r['teacher_name']} - Period {r['period']} ({r['period_time']})\n"
                                if r['class']:
                                    summary += f"   └ Class: {r['class']}\n"
                            
                            if len(created_reminders) > 10:
                                summary += f"\n... and {len(created_reminders) - 10} more\n"
                            
                            summary += f"\n*Matched to users:* {matched_count}\n"
                            summary += f"*Not matched:* {unmatched_count}\n\n"
                            summary += "_Reminders will be sent 5 minutes before each period._"
                            
                            # Create activation buttons
                            keyboard = [
                                [InlineKeyboardButton("✅ Activate All Matched", callback_data="relief_activate_all")],
                                [InlineKeyboardButton("🔧 Select Individual", callback_data="relief_select_individual")],
                                [InlineKeyboardButton("❌ Skip Reminders", callback_data="relief_skip")],
                            ]
                            
                            await update.message.reply_text(
                                summary,
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            
                            # Don't clear user_data - we need it for the next state
                            return RELIEF_ACTIVATION
                        else:
                            await update.message.reply_text(
                                "✅ Relief information saved.\n\n"
                                "⚠️ Could not create reminders from the data.\n"
                                "Use /upload to add more.",
                            )
                    else:
                        await update.message.reply_text(
                            "✅ Relief information saved.\n\n"
                            "ℹ️ No structured relief data could be extracted for reminders.\n"
                            "Use /upload to add more.",
                        )
                except Exception as e:
                    logger.error(f"Error in relief processing: {e}", exc_info=True)
                    await update.message.reply_text(
                        f"✅ Relief information saved.\n\n"
                        f"⚠️ Error setting up reminders: {str(e)}\n"
                        f"Use /reliefstatus to check manually.",
                    )
            else:
                # Non-admin uploaded RELIEF
                await update.message.reply_text(
                    f"✅ *Information saved!*\n\n"
                    f"Category: {selected_tag}\n"
                    f"Type: {content_type}\n\n"
                    f"Use /upload to add more.",
                    parse_mode="Markdown",
                )
        else:
            # Non-RELIEF upload - show normal confirmation
            await update.message.reply_text(
                f"✅ *Information saved!*\n\n"
                f"Category: {selected_tag}\n"
                f"Type: {content_type}\n\n"
                f"Use /upload to add more.",
                parse_mode="Markdown",
            )

        context.user_data.clear()
        return ConversationHandler.END

    async def cancel_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel upload process"""
        await update.message.reply_text(
            "Upload cancelled.", reply_markup=ReplyKeyboardRemove()
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def cancel_upload_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel upload process from callback"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Upload cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    async def handle_upload_menu_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages sent while in upload menu - remind user to use buttons"""
        await update.message.reply_text(
            "⚠️ Please use the buttons above to select an option.\n\n"
            "Or send /cancel to exit."
        )
        return UPLOAD_MENU

    async def handle_privacy_warning_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages sent while in privacy warning - remind user to use buttons"""
        await update.message.reply_text(
            "⚠️ Please click 'I Agree' or 'Cancel' above to continue.\n\n"
            "Or send /cancel to exit."
        )
        return PRIVACY_WARNING

    async def handle_delete_menu_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages sent while in delete menu - remind user to use buttons"""
        await update.message.reply_text(
            "⚠️ Please use the buttons above to select which upload to delete.\n\n"
            "Or send /cancel to exit."
        )
        return SELECTING_UPLOAD_TO_DELETE

    async def handle_relief_activation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle relief reminder activation choices"""
        query = update.callback_query
        await query.answer()
        
        action = query.data
        
        if action == "relief_activate_all":
            # Activate all matched reminders
            activated = db.activate_all_matched_reminders()
            await query.edit_message_text(
                f"✅ *Activated {activated} relief reminders!*\n\n"
                f"Teachers will receive notifications {REMINDER_MINUTES_BEFORE} minutes before their relief period.\n\n"
                f"Use /reliefstatus to view active reminders.\n"
                f"Use /cancelrelief to cancel reminders.",
                parse_mode="Markdown"
            )
            context.user_data.clear()
            return ConversationHandler.END
            
        elif action == "relief_select_individual":
            # Show individual selection
            reminders = context.user_data.get("pending_relief_reminders", [])
            
            if not reminders:
                await query.edit_message_text("No reminders available to select.")
                context.user_data.clear()
                return ConversationHandler.END
            
            # Create buttons for each reminder
            keyboard = []
            for r in reminders:
                if r["matched"]:
                    status = "🔔" if r.get("activated") else "🔕"
                    text = f"{status} {r['teacher_name']} - P{r['period']}"
                    keyboard.append([InlineKeyboardButton(text, callback_data=f"relief_toggle_{r['id']}")])
            
            keyboard.append([InlineKeyboardButton("✅ Save & Activate", callback_data="relief_save_selection")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="relief_skip")])
            
            await query.edit_message_text(
                "🔧 *Select reminders to activate:*\n\n"
                "Tap to toggle on/off. Only matched users shown.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return SELECTING_RELIEF_REMINDERS
            
        elif action == "relief_skip":
            await query.edit_message_text(
                "ℹ️ Relief reminders skipped.\n"
                "Use /reliefstatus to view or activate reminders later.",
            )
            context.user_data.clear()
            return ConversationHandler.END
        
        return RELIEF_ACTIVATION

    async def handle_relief_individual_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle individual relief reminder selection"""
        query = update.callback_query
        await query.answer()
        
        action = query.data
        
        if action == "relief_save_selection":
            # Count activated reminders
            reminders = db.get_today_relief_reminders()
            activated = sum(1 for r in reminders if r["activated"])
            
            await query.edit_message_text(
                f"✅ *Saved! {activated} reminders activated.*\n\n"
                f"Use /reliefstatus to view active reminders.\n"
                f"Use /cancelrelief to cancel reminders.",
                parse_mode="Markdown"
            )
            context.user_data.clear()
            return ConversationHandler.END
            
        elif action == "relief_skip":
            await query.edit_message_text(
                "ℹ️ Relief reminders cancelled.",
            )
            context.user_data.clear()
            return ConversationHandler.END
            
        elif action.startswith("relief_toggle_"):
            reminder_id = int(action.replace("relief_toggle_", ""))
            
            # Toggle the reminder activation
            reminder = db.get_relief_reminder_by_id(reminder_id)
            if reminder:
                new_state = not reminder["activated"]
                db.activate_reminder(reminder_id, new_state)
            
            # Refresh the button list
            reminders = db.get_today_relief_reminders()
            keyboard = []
            
            for r in reminders:
                if r["teacher_telegram_id"]:
                    status = "🔔" if r["activated"] else "🔕"
                    text = f"{status} {r['teacher_name']} - P{r['period']}"
                    keyboard.append([InlineKeyboardButton(text, callback_data=f"relief_toggle_{r['id']}")])
            
            keyboard.append([InlineKeyboardButton("✅ Save & Activate", callback_data="relief_save_selection")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="relief_skip")])
            
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            return SELECTING_RELIEF_REMINDERS
        
        return SELECTING_RELIEF_REMINDERS

    async def relief_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current relief reminder status"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        
        if not user or user["role"] not in ["relief_member", "admin", "superadmin"]:
            await update.message.reply_text("❌ This command is for relief members and admins only.")
            return
        
        reminders = db.get_today_relief_reminders()
        
        if not reminders:
            await update.message.reply_text(
                "📋 *No relief reminders for today.*\n\n"
                "Upload RELIEF information to create reminders.",
                parse_mode="Markdown"
            )
            return
        
        message = "📋 *Today's Relief Reminders:*\n\n"
        
        active_count = 0
        for r in reminders:
            status = "🔔" if r["activated"] else "🔕"
            sent = " (sent)" if r["reminder_sent"] else ""
            matched = "✓" if r["teacher_telegram_id"] else "?"
            
            if r["activated"]:
                active_count += 1
            
            message += f"{status} [{matched}] {r['teacher_name']} - P{r['period']} ({r['relief_time']}){sent}\n"
            if r['class_info']:
                message += f"   └ {r['class_info']}"
                if r['room']:
                    message += f" @ {r['room']}"
                message += "\n"
        
        message += f"\n*Active:* {active_count}/{len(reminders)}\n"
        message += f"_✓ = matched to user, ? = not matched_"
        
        # Add action buttons
        keyboard = [
            [InlineKeyboardButton("✅ Activate All Matched", callback_data="relief_cmd_activate_all")],
            [InlineKeyboardButton("❌ Deactivate All", callback_data="relief_cmd_deactivate_all")],
        ]
        
        await update.message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_relief_command_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle relief status command callbacks"""
        query = update.callback_query
        await query.answer()
        
        action = query.data
        
        if action == "relief_cmd_activate_all":
            activated = db.activate_all_matched_reminders()
            await query.edit_message_text(
                f"✅ Activated {activated} relief reminders.",
            )
        elif action == "relief_cmd_deactivate_all":
            deactivated = db.deactivate_all_reminders_today()
            await query.edit_message_text(
                f"❌ Deactivated {deactivated} relief reminders.",
            )

    async def cancel_relief(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel all relief reminders for today"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        
        if not user or user["role"] not in ["relief_member", "admin", "superadmin"]:
            await update.message.reply_text("❌ This command is for relief members and admins only.")
            return
        
        deactivated = db.deactivate_all_reminders_today()
        
        await update.message.reply_text(
            f"❌ *Cancelled {deactivated} relief reminders.*\n\n"
            f"Use /reliefstatus to reactivate if needed.",
            parse_mode="Markdown"
        )

    # ===== GOOGLE DRIVE SYNC =====

    async def set_folder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set folder-role access mapping (superadmin only)"""
        try:
            user_id = update.effective_user.id
            user = db.get_user(user_id)
            
            if not user or user["role"] != "superadmin":
                await update.message.reply_text("❌ Only super admins can configure folders.")
                return
            
            if not self.drive_sync:
                await update.message.reply_text("❌ Google Drive is not configured.")
                return
            
            if len(context.args) < 2:
                await update.message.reply_text(
                    "Usage: /setfolder \"Folder Name\" role1,role2\n\n"
                    "Example: /setfolder \"Relief Committee\" relief_member,admin\n\n"
                    "Available roles: viewer, relief_member, admin, superadmin\n\n"
                    "⚠️ Use *quotes* around folder names with spaces (e.g. \"Relief Committee\").",
                    parse_mode="Markdown"
                )
                return
            
            valid_roles = ["viewer", "relief_member", "admin", "student_admin", "superadmin"]
            raw_args = [a.strip('"\'') for a in context.args]
            
            # Parse: folder name may be one or more words; rest are role1,role2,...
            folder_name = None
            roles = None
            # If first arg looks like a single quoted folder name (no comma), use it and rest as roles
            if len(raw_args) >= 2:
                roles_str = " ".join(raw_args[1:])
                roles = [r.strip() for r in roles_str.split(",")]
                invalid = [r for r in roles if r not in valid_roles]
                if not invalid:
                    folder_name = raw_args[0]
            
            # If that failed (e.g. multi-word folder without quotes), try joining words until roles are valid
            if folder_name is None and len(raw_args) >= 2:
                for i in range(1, len(raw_args)):
                    folder_name = " ".join(raw_args[:i])
                    roles_str = " ".join(raw_args[i:])
                    roles = [r.strip() for r in roles_str.split(",")]
                    invalid = [r for r in roles if r not in valid_roles]
                    if not invalid:
                        break
                else:
                    folder_name = None
                    roles = [r.strip() for r in " ".join(raw_args[1:]).split(",")]
                    invalid_roles = [r for r in roles if r not in valid_roles]
                    await update.message.reply_text(
                        f"❌ Invalid roles: {', '.join(invalid_roles)}\n\n"
                        f"Valid roles: {', '.join(valid_roles)}\n\n"
                        f"💡 If the folder name has spaces, use quotes: /setfolder \"Relief Committee\" relief_member,admin"
                    )
                    return
            
            if folder_name is None or not roles:
                await update.message.reply_text(
                    "❌ Could not parse folder name and roles. Use: /setfolder \"Folder Name\" role1,role2"
                )
                return
            
            # Find folder in Drive
            folder = self.drive_sync.get_folder_by_name(folder_name)
            if not folder:
                await update.message.reply_text(
                    f"❌ Folder '{folder_name}' not found in Google Drive.\n\n"
                    f"Use /listfolders to see available folders.\n\n"
                    f"💡 Check spelling (e.g. \"Committee\" not \"Commitee\")."
                )
                return
            
            # Add/update folder in database
            folder_id = db.add_or_update_drive_folder(
                folder_name=folder['name'],
                drive_folder_id=folder['id'],
                parent_folder_id=GOOGLE_DRIVE_ROOT_FOLDER_ID
            )
            
            # Set role access
            db.set_folder_role_access(folder_id, roles)
            
            # Use HTML parse mode to avoid Markdown parsing issues with underscores and special chars
            await update.message.reply_text(
                f"✅ <b>Folder configured!</b>\n\n"
                f"<b>Folder:</b> {folder['name']}\n"
                f"<b>Accessible to:</b> {', '.join(roles)}\n\n"
                f"Use /sync to sync files from this folder.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error in set_folder command: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Error configuring folder: {str(e)[:200]}\n\n"
                f"Please check:\n"
                f"• Folder name spelling\n"
                f"• Database connection\n"
                f"• Google Drive API access"
            )

    async def list_folders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all folders and their role access"""
        try:
            user_id = update.effective_user.id
            user = db.get_user(user_id)
            
            if not user or user["role"] not in ["admin", "superadmin"]:
                await update.message.reply_text("❌ This command is for admins only.")
                return
            
            if not self.drive_sync:
                await update.message.reply_text("❌ Google Drive is not configured.")
                return
            
            # Get folders from Drive
            drive_folders = self.drive_sync.list_folders()
            db_folders = db.get_all_folders()
            
            if not drive_folders:
                await update.message.reply_text("📁 No folders found in Google Drive.")
                return
            
            # Use HTML parse mode to avoid Markdown parsing issues
            message = "📁 <b>Google Drive Folders:</b>\n\n"
            
            for folder in drive_folders:
                folder_name = folder['name']
                # Escape HTML special characters in folder name
                folder_name_escaped = folder_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                
                # Check if configured in database
                db_folder = db.get_folder_by_drive_id(folder['id'])
                
                if db_folder:
                    folder_with_roles = db.get_folder_with_roles(db_folder['id'])
                    roles = folder_with_roles.get('roles', [])
                    if roles:
                        message += f"✅ <b>{folder_name_escaped}</b>\n"
                        message += f"   └ Roles: {', '.join(roles)}\n\n"
                    else:
                        message += f"⚠️ <b>{folder_name_escaped}</b>\n"
                        message += f"   └ No roles configured\n\n"
                else:
                    message += f"❌ <b>{folder_name_escaped}</b>\n"
                    message += f"   └ Not configured (use /setfolder)\n\n"
            
            await update.message.reply_text(message, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error in list_folders command: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Error listing folders: {str(e)[:200]}\n\n"
                f"Please check:\n"
                f"• Database connection\n"
                f"• Google Drive API access"
            )

    async def sync_drive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Sync files from Google Drive (role-based)"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        
        if not user:
            await update.message.reply_text("❌ You need to be registered. Use /start first.")
            return
        
        if not self.drive_sync:
            await update.message.reply_text("❌ Google Drive is not configured.")
            return
        
        await update.message.reply_text("🔄 Syncing files from Google Drive...")
        
        # Check role access (student_admin syncs Student Movement via Telegram only, not Drive)
        if user["role"] not in ["relief_member", "admin", "superadmin"]:
            await update.message.reply_text(
                f"❌ Only relief_member, admin, and superadmin can sync Google Drive folders.\n"
                f"Viewers and student_admin can only query data."
            )
            return
        
        # Get folders from database
        all_folders = db.get_all_folders()
        
        # If no folders in database, auto-discover from Google Drive
        if not all_folders:
            await update.message.reply_text("📁 Discovering folders from Google Drive...")
            drive_folders = self.drive_sync.list_folders()
            
            if not drive_folders:
                await update.message.reply_text("❌ No folders found in Google Drive.")
                return
            
            # Auto-add all discovered folders to database
            for drive_folder in drive_folders:
                db.add_or_update_drive_folder(
                    folder_name=drive_folder['name'],
                    drive_folder_id=drive_folder['id'],
                    parent_folder_id=GOOGLE_DRIVE_ROOT_FOLDER_ID
                )
            
            all_folders = db.get_all_folders()
            await update.message.reply_text(
                f"✅ Discovered {len(all_folders)} folders. Starting sync...\n"
                f"💡 Use /setfolder to configure role access if needed."
            )
        
        # All folders except Student Movement (Telegram-only) are synced from Drive
        accessible_folders = [f for f in all_folders if f['folder_name'] != 'Student Movement']
        
        total_files = 0
        total_processed = 0
        errors = []
        
        for folder in accessible_folders:
            try:
                folder_name = folder['folder_name']
                drive_folder_id = folder['drive_folder_id']
                
                await update.message.reply_text(f"📂 Processing folder: {folder_name}...")
                
                # List files in folder
                files = self.drive_sync.list_files_in_folder(drive_folder_id)
                
                # Today's Event: only PDFs named dd_mm_yy_eventname.pdf where date = today
                if folder_name == "Today's Event" and files:
                    today = get_singapore_now().date()
                    filtered = []
                    for f in files:
                        is_match, event_name = self._is_todays_event_pdf(f.get('name', ''))
                        if is_match:
                            f['_event_name'] = event_name
                            filtered.append(f)
                    files = filtered
                    if not files:
                        await update.message.reply_text(
                            f"📂 {folder_name}: No PDFs with today's date ({today.strftime('%d/%m/%Y')}) found. Skipping."
                        )
                        continue
                
                if not files:
                    continue
                
                files_synced = len(files)
                files_processed_count = 0
                
                for file in files:
                    try:
                        # Get file content
                        file_content = self.drive_sync.get_file_content(file)
                        
                        if not file_content:
                            errors.append(f"{file['name']}: Failed to download")
                            continue
                        
                        # Detect category
                        category = self.drive_sync.detect_file_category(file['name'], folder_name)
                        
                        # Process based on file type
                        extracted_text = ""
                        file_type = "document"
                        
                        if file.get('mimeType', '').startswith('image/'):
                            # Image file
                            extracted_text = self.analyze_image(file_content, category)
                            file_type = "photo"
                        elif file.get('mimeType', '') == 'application/pdf' or file['name'].lower().endswith('.pdf'):
                            # PDF file
                            extracted_text = self.analyze_pdf(file_content, category)
                            file_type = "document"
                        elif file.get('mimeType', '') == 'application/vnd.google-apps.spreadsheet':
                            # Google Sheets exported as CSV - read directly
                            try:
                                extracted_text = file_content.decode('utf-8')
                                logger.info(f"Read Google Sheets as CSV: {len(extracted_text)} chars")
                            except:
                                extracted_text = file_content.decode('latin-1')
                            file_type = "document"
                        elif file.get('mimeType', '').startswith('text/'):
                            # Text file (including CSV)
                            try:
                                extracted_text = file_content.decode('utf-8')
                            except:
                                extracted_text = file_content.decode('latin-1')
                            file_type = "document"
                        else:
                            # Try to extract text from PDF (if exported from Google Docs)
                            if file_content[:4] == b'%PDF':
                                extracted_text = self.analyze_pdf(file_content, category)
                            else:
                                # Try as text
                                try:
                                    extracted_text = file_content.decode('utf-8')
                                except:
                                    extracted_text = f"[Binary file: {file['name']}]"
                        
                        # Save to database (upsert by drive_file_id: one entry per file per day)
                        content_data = {
                            "type": file_type,
                            "file_name": file['name'],
                            "extracted_text": extracted_text,
                            "source": "google_drive",
                            "folder": folder_name,
                            "drive_folder_id": drive_folder_id,  # Store for access control
                            "drive_file_id": file.get('id'),  # For upsert
                        }
                        if folder_name == "Today's Event" and file.get('_event_name'):
                            content_data["event_name"] = file['_event_name']
                        if content_data.get("drive_file_id"):
                            db.add_or_update_drive_entry(user_id, category, content_data)
                        else:
                            db.add_entry(user_id, category, content_data)
                        files_processed_count += 1
                        
                    except Exception as e:
                        logger.error(f"Error processing file {file['name']}: {e}")
                        errors.append(f"{file['name']}: {str(e)}")
                
                # Update sync time
                db.update_folder_sync_time(folder['id'])
                
                # Log sync
                error_str = "; ".join(errors[-10:]) if errors else None  # Last 10 errors
                db.log_sync(
                    folder_id=folder['id'],
                    files_synced=files_synced,
                    files_processed=files_processed_count,
                    errors=error_str,
                    synced_by=user_id
                )
                
                total_files += files_synced
                total_processed += files_processed_count
                
            except Exception as e:
                logger.error(f"Error syncing folder {folder.get('folder_name', 'unknown')}: {e}")
                errors.append(f"Folder {folder.get('folder_name', 'unknown')}: {str(e)}")
        
        # Report results
        message = f"✅ *Sync Complete!*\n\n"
        message += f"*Folders processed:* {len(accessible_folders)}\n"
        message += f"*Files found:* {total_files}\n"
        message += f"*Files processed:* {total_processed}\n"
        
        if errors:
            message += f"\n*Errors:* {len(errors)}\n"
            if len(errors) <= 5:
                message += "\n".join([f"• {e}" for e in errors])
            else:
                message += "\n".join([f"• {e}" for e in errors[:5]])
                message += f"\n... and {len(errors) - 5} more"
        
        await update.message.reply_text(message, parse_mode="Markdown")

    async def drive_folder_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show connected Google Drive folder info"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        
        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text("❌ This command is for admins only.")
            return
        
        if not self.drive_sync:
            await update.message.reply_text("❌ Google Drive is not configured.")
            return
        
        message = "📁 *Google Drive Configuration*\n\n"
        message += f"*Root Folder ID:* `{GOOGLE_DRIVE_ROOT_FOLDER_ID}`\n\n"
        
        # List folders
        folders = self.drive_sync.list_folders()
        message += f"*Folders found:* {len(folders)}\n"
        
        if folders:
            message += "\n*Available folders:*\n"
            for folder in folders[:10]:
                message += f"• {folder['name']}\n"
            if len(folders) > 10:
                message += f"... and {len(folders) - 10} more\n"
        
        message += "\n*Sync Schedule:*\n"
        for folder_name, (h, m) in SYNC_SCHEDULE.items():
            message += f"• {folder_name}: {h:02d}:{m:02d} SGT daily\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")

    async def assume_role(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Assume a different role for testing (superadmin only)"""
        user_id = update.effective_user.id
        
        # Check if user is superadmin (check both config and actual database role)
        is_protected_superadmin = user_id in SUPER_ADMIN_IDS
        
        # Get actual role from database (not assumed role)
        conn = db.get_connection()
        cursor = conn.cursor(row_factory=dict_row)
        cursor.execute(
            "SELECT role FROM users WHERE telegram_id = %s",
            (user_id,)
        )
        user_row = cursor.fetchone()
        cursor.close()
        conn.close()
        
        is_superadmin_role = user_row and user_row['role'] == 'superadmin' if user_row else False
        
        if not (is_protected_superadmin or is_superadmin_role):
            await update.message.reply_text(
                "❌ This command is for superadmins only."
            )
            return
        
        # Get the role to assume from command arguments
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ *Usage:* `/assume [role]`\n\n"
                "Available roles:\n"
                "• `viewer`\n"
                "• `relief_member`\n"
                "• `admin`\n"
                "• `student_admin`\n\n"
                "Example: `/assume viewer`",
                parse_mode="Markdown"
            )
            return
        
        role_to_assume = context.args[0].lower()
        valid_roles = ['viewer', 'relief_member', 'admin', 'student_admin']
        
        if role_to_assume not in valid_roles:
            await update.message.reply_text(
                f"❌ Invalid role: `{role_to_assume}`\n\n"
                f"Valid roles: {', '.join(valid_roles)}",
                parse_mode="Markdown"
            )
            return
        
        # Get original role (before any assumption)
        # First check if there's already an assumption
        existing_assumption = db.get_role_assumption(user_id)
        if existing_assumption:
            # Already assuming a role, use the stored original
            original_role = existing_assumption['original_role']
        else:
            # No assumption yet, get from user table
            user = db.get_user(user_id)
            if not user:
                await update.message.reply_text("❌ User not found in database.")
                return
            # Get the actual role from database (not the effective role)
            conn = db.get_connection()
            cursor = conn.cursor(row_factory=dict_row)
            cursor.execute(
                "SELECT role FROM users WHERE telegram_id = %s",
                (user_id,)
            )
            user_row = cursor.fetchone()
            cursor.close()
            conn.close()
            original_role = user_row['role'] if user_row else 'superadmin'
        
        # Store assumption
        db.assume_role(user_id, role_to_assume, original_role)
        
        await update.message.reply_text(
            f"✅ *Role Assumed*\n\n"
            f"*Original role:* {original_role}\n"
            f"*Assumed role:* {role_to_assume}\n\n"
            f"You now have the permissions of a `{role_to_assume}`.\n"
            f"Use `/resume` to restore your original role.",
            parse_mode="Markdown"
        )

    async def resume_role(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume original superadmin role"""
        user_id = update.effective_user.id
        
        # Check if user is superadmin (check both config and database role)
        is_protected_superadmin = user_id in SUPER_ADMIN_IDS
        user = db.get_user(user_id)
        
        # Get true original role
        assumption = db.get_role_assumption(user_id)
        if not assumption:
            await update.message.reply_text(
                "ℹ️ You are not currently assuming any role.\n"
                "Your current role is your actual role."
            )
            return
        
        original_role = assumption['original_role']
        
        # Verify user is actually a superadmin (check database role, not assumed)
        conn = db.get_connection()
        cursor = conn.cursor(row_factory=dict_row)
        cursor.execute(
            "SELECT role FROM users WHERE telegram_id = %s",
            (user_id,)
        )
        user_row = cursor.fetchone()
        cursor.close()
        conn.close()
        
        actual_role = user_row['role'] if user_row else None
        is_superadmin_role = actual_role == 'superadmin'
        
        if not (is_protected_superadmin or is_superadmin_role):
            await update.message.reply_text(
                "❌ This command is for superadmins only."
            )
            return
        
        # Resume original role
        db.resume_role(user_id)
        
        await update.message.reply_text(
            f"✅ *Role Resumed*\n\n"
            f"*Restored role:* {original_role}\n\n"
            f"You now have your original superadmin permissions back.",
            parse_mode="Markdown"
        )

    async def sync_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show sync status for today"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        
        if not user:
            await update.message.reply_text("❌ You need to be registered. Use /start first.")
            return
        
        logs = db.get_today_sync_logs()
        
        if not logs:
            await update.message.reply_text("📊 *No syncs today.*\n\nUse /sync to sync files.", parse_mode="Markdown")
            return
        
        message = "📊 *Today's Sync Status:*\n\n"
        
        for log in logs[:10]:
            folder_name = log.get('folder_name', 'Unknown')
            message += f"*{folder_name}* ({log.get('synced_at', '?')})\n"
            message += f"  Files: {log.get('files_synced', 0)} found, {log.get('files_processed', 0)} processed\n"
            if log.get('errors'):
                message += f"  ⚠️ Errors: {log['errors'][:50]}...\n"
            message += "\n"
        
        if len(logs) > 10:
            message += f"... and {len(logs) - 10} more syncs"
        
        await update.message.reply_text(message, parse_mode="Markdown")

    async def ask_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle queries with Claude"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user:
            await update.message.reply_text(
                "❌ You need to be registered. Use /start first."
            )
            return

        # Extract query
        query = " ".join(context.args) if context.args else ""

        if not query:
            await update.message.reply_text(
                "Please provide a question.\n\n" "Example: /ask Who's teaching 3A at 10am?"
            )
            return

        await update.message.reply_text("🔍 Searching today's information...")

        # Get today's entries
        all_entries = db.get_today_entries()
        logger.debug(f"Retrieved {len(all_entries)} total entries from database")

        # Filter entries based on folder access rules
        # Use effective role (respects role assumptions)
        user_role = user.get("role", "viewer")
        # Log for debugging role assumption issues
        is_assumed = user.get('is_assumed', False)
        if is_assumed:
            logger.info(f"User {user_id} has assumed role '{user_role}' (original: {user.get('original_role', 'unknown')}, effective_role: {user.get('effective_role', 'unknown')})")
        else:
            logger.debug(f"User {user_id} using role '{user_role}' (no assumption)")
        
        entries = self._filter_entries_by_folder_access(all_entries, user_role)
        logger.info(f"After filtering: {len(entries)} entries accessible to role '{user_role}' (from {len(all_entries)} total)")

        if not entries:
            await update.message.reply_text(
                "📭 No information accessible for your role today."
            )
            return

        # Build context for Claude
        context_text = self._build_context_for_claude(entries, query)

        # Query Claude (include Singapore time so "today" is clear)
        sgt_str = get_singapore_date_time_str()
        try:
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[
                    {
                        "role": "user",
                        "content": f"""You are a helpful school admin assistant. Based on today's information, answer the following question concisely.

CURRENT DATE/TIME (Singapore): {sgt_str}
When the user says "today", it means this date in Singapore time.

TODAY'S INFORMATION:
{context_text}

QUESTION: {query}

Provide a direct, concise answer based only on the information above. If the documents clearly list someone or something for a date, say so; do not state 'no request' or 'not listed' when the text explicitly shows otherwise. If the information isn't in the documents, say so clearly.""",
                    }
                ],
            )

            answer = response.content[0].text

            await update.message.reply_text(f"💡 *Answer:*\n\n{answer}", parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            await update.message.reply_text(
                "❌ Sorry, I encountered an error processing your query.\n\n"
                f"Raw entries found: {len(entries)}"
            )

    def _is_student_movement_entry(self, entry):
        """Check if entry is Student Movement (tag or folder)."""
        tag = entry.get('tag', '')
        if tag == 'STUDENT_MOVEMENT':
            return True
        content = entry.get('content', {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                content = {}
        folder = content.get('folder', '') if isinstance(content, dict) else ''
        return folder and 'Student Movement' in folder

    def _filter_entries_by_folder_access(self, entries, user_role):
        """
        Filter entries based on folder access rules:
        - Each entry has a drive_folder_id in content_data (or tag for Student Movement)
        - Check if user's role has access to that folder
        - student_admin: access only to Student Movement entries
        - Superadmins have access to all folders (unless they've assumed a different role)
        """
        if not entries:
            return entries
        
        # Superadmins can access everything ONLY if their effective role is superadmin
        if user_role == 'superadmin':
            logger.debug("Superadmin role detected - allowing access to all entries")
            return entries
        
        # student_admin: only Student Movement entries (uploaded via Telegram)
        if user_role == 'student_admin':
            filtered = [e for e in entries if self._is_student_movement_entry(e)]
            logger.debug(f"student_admin: {len(filtered)}/{len(entries)} Student Movement entries")
            return filtered
        
        filtered_entries = []
        stats = {
            'no_folder_id': 0,
            'folder_not_found': 0,
            'no_roles_set': 0,
            'role_allowed': 0,
            'role_denied': 0
        }
        
        for entry in entries:
            # Handle content field - it might be a dict (from JSONB) or a string
            content = entry.get('content', {})
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    content = {}
            
            drive_folder_id = content.get('drive_folder_id') if isinstance(content, dict) else None
            
            if not drive_folder_id:
                # Entry doesn't have folder info (e.g., manual upload)
                # relief_member, admin have access; viewers do not
                if user_role in ['relief_member', 'admin']:
                    filtered_entries.append(entry)
                    stats['no_folder_id'] += 1
                else:
                    stats['role_denied'] += 1
                continue
            
            # Get folder from database
            folder = db.get_folder_by_drive_id(drive_folder_id)
            if not folder:
                if user_role in ['relief_member', 'admin']:
                    filtered_entries.append(entry)
                    stats['folder_not_found'] += 1
                else:
                    stats['role_denied'] += 1
                continue
            
            # Today's Event folder: accessible to everyone (viewers included)
            if folder.get('folder_name') == "Today's Event":
                filtered_entries.append(entry)
                stats['role_allowed'] += 1
                continue
            
            # Check if user's role has access to this folder
            folder_with_roles = db.get_folder_with_roles(folder['id'])
            if not folder_with_roles or not folder_with_roles.get('roles'):
                if user_role in ['relief_member', 'admin']:
                    filtered_entries.append(entry)
                    stats['no_roles_set'] += 1
                else:
                    stats['role_denied'] += 1
                continue
            
            allowed_roles = folder_with_roles.get('roles', [])
            if user_role in allowed_roles:
                filtered_entries.append(entry)
                stats['role_allowed'] += 1
            else:
                stats['role_denied'] += 1
        
        logger.info(f"Filter stats for role '{user_role}': {stats}")
        return filtered_entries

    def _build_context_for_claude(self, entries, query):
        """Build context string from entries, filtering by relevance"""
        context_parts = []

        for entry in entries:
            tag = entry["tag"]
            content_data = entry["content"]
            timestamp = entry["timestamp"]

            # Format entry
            entry_text = f"[{tag}] at {timestamp}:\n"

            if content_data["type"] == "text":
                entry_text += content_data["content"]
            elif content_data["type"] in ["photo", "document"]:
                caption = content_data.get("caption", "")
                extracted_text = content_data.get("extracted_text", "")
                
                entry_text += f"[{content_data['type'].upper()}]"
                if caption:
                    entry_text += f"\nCaption: {caption}"
                if extracted_text:
                    entry_text += f"\nExtracted content:\n{extracted_text}"

            context_parts.append(entry_text)

        return "\n\n".join(context_parts)

    def _filter_entries_by_today_menu(self, entries, menu_key):
        """Filter entries for a specific /today menu option."""
        if menu_key == "relief":
            return [e for e in entries if e.get("tag") == "RELIEF" or self._entry_folder_contains(e, "Relief")]
        if menu_key == "weekly_bulletin":
            return [e for e in entries if self._entry_folder_contains(e, "Weekly Bulletin")]
        if menu_key == "student_movement":
            return [e for e in entries if self._is_student_movement_entry(e)]
        if menu_key == "this_week_ctss":
            # This Week@CTSS: Weekly Bulletin content (same as weekly bulletin)
            return [e for e in entries if self._entry_folder_contains(e, "Weekly Bulletin")]
        if menu_key == "event":
            return [e for e in entries if e.get("tag") == "EVENT" or self._entry_folder_contains(e, "Today's Event")]
        return entries

    def _entry_folder_contains(self, entry, folder_substring):
        """Check if entry's folder contains the given substring."""
        content = entry.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                content = {}
        folder = content.get("folder", "") if isinstance(content, dict) else ""
        return folder_substring.lower() in (folder or "").lower()

    async def today_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's menu with clickable options: Relief, Weekly Bulletin, Student Movement, This Week@CTSS, Event"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user:
            await update.message.reply_text("❌ Not registered. Use /start first.")
            return

        all_entries = db.get_today_entries()
        user_role = user.get("role", "viewer")
        entries = self._filter_entries_by_folder_access(all_entries, user_role)

        if not entries:
            await update.message.reply_text("📭 No information accessible for your role today.")
            return

        sgt_str = get_singapore_date_time_str()
        message = f"📊 *TODAY'S INFORMATION* ({sgt_str})\n\n"
        message += "Select a category for more details:\n\n"

        # Menu options: Today's Relief, Today@Weekly Bulletin, Today's Student Movement, This Week@CTSS, Today's Event
        menu_options = [
            ("relief", "Today's Relief"),
            ("weekly_bulletin", "Today@Weekly Bulletin"),
            ("student_movement", "Today's Student Movement"),
            ("this_week_ctss", "This Week@CTSS"),
            ("event", "Today's Event"),
        ]

        buttons = []
        for key, label in menu_options:
            filtered = self._filter_entries_by_today_menu(entries, key)
            count = len(filtered)
            emoji = "📋" if count > 0 else "⚪️"
            buttons.append([InlineKeyboardButton(f"{emoji} {label} ({count})", callback_data=f"summary_{key}")])

        buttons.append([InlineKeyboardButton("📝 Full Summary (All)", callback_data="summary_ALL")])

        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(message, parse_mode="Markdown", reply_markup=keyboard)

    async def handle_summary_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback when user clicks a summary button"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user = db.get_user(user_id)
        
        if not user:
            await query.edit_message_text("❌ Not registered.")
            return
        
        # Extract category from callback data
        callback_data = query.data
        category = callback_data.replace("summary_", "")
        
        await query.edit_message_text("🔍 Generating summary... Please wait.")
        
        # Get entries and filter by folder access
        all_entries = db.get_today_entries()
        user_role = user.get("role", "viewer")
        entries = self._filter_entries_by_folder_access(all_entries, user_role)
        
        if category != "ALL":
            if category in ("relief", "weekly_bulletin", "student_movement", "this_week_ctss", "event"):
                entries = self._filter_entries_by_today_menu(entries, category)
            elif category in TAGS:
                entries = [e for e in entries if e.get("tag") == category]
            else:
                entries = []
        
        if not entries:
            category_label = {
                "relief": "Today's Relief",
                "weekly_bulletin": "Today@Weekly Bulletin",
                "student_movement": "Today's Student Movement",
                "this_week_ctss": "This Week@CTSS",
                "event": "Today's Event",
            }.get(category, category)
            await query.edit_message_text(f"📭 No entries found for {category_label}.")
            return
        
        # Build context for Claude
        context_text = self._build_context_for_claude(entries, "summary")
        
        # Generate summary with Claude (include Singapore time for "today" context)
        sgt_str = get_singapore_date_time_str()
        try:
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Current date/time (Singapore): {sgt_str}. "Today" refers to this date.

Based on the following school information entries, provide a clear and organized summary of the MAIN POINTS.

Format your response as bullet points grouped by category if there are multiple categories.
Focus on key information like: names, times, classes, rooms, and any important details.
Be concise but comprehensive.

TODAY'S ENTRIES:
{context_text}

Provide a summary of the main points:"""
                    }
                ],
            )
            
            summary_text = response.content[0].text
            
            # Format response
            category_labels = {
                "relief": "Today's Relief",
                "weekly_bulletin": "Today@Weekly Bulletin",
                "student_movement": "Today's Student Movement",
                "this_week_ctss": "This Week@CTSS",
                "event": "Today's Event",
            }
            if category == "ALL":
                header = "📝 *FULL SUMMARY - ALL CATEGORIES*\n\n"
            else:
                header = f"📋 *{category_labels.get(category, category)}*\n\n"
            
            await query.edit_message_text(
                f"{header}{summary_text}",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Summary generation error: {e}")
            await query.edit_message_text(f"❌ Error generating summary: {str(e)[:100]}")

    async def get_upload_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's upload code to authorized users"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text("❌ You don't have upload permissions.")
            return

        code = db.get_daily_code()
        await update.message.reply_text(
            f"🔐 *Today's Upload Code:*\n\n`{code}`\n\n"
            f"Valid until midnight (SGT)",
            parse_mode="Markdown",
        )

    async def add_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a new viewer - with confirmation"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text("❌ You don't have permission to add users.")
            return

        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /add [telegram_id] [name]\n\n"
                "Example: /add 123456789 John Teacher"
            )
            return

        try:
            new_user_id = int(context.args[0])
            # Join remaining args as name (allows spaces in names)
            display_name = " ".join(context.args[1:])

            # Check if user already exists
            existing = db.get_user(new_user_id)
            if existing:
                await update.message.reply_text(
                    f"❌ User {new_user_id} is already registered as {existing['role']}."
                )
                return

            # Store pending action and ask for confirmation
            buttons = [
                [InlineKeyboardButton("✅ Confirm Add", callback_data=f"admin_add_confirm_{new_user_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            
            # Store display name in context for later
            context.user_data["pending_add_name"] = display_name
            context.user_data["pending_add_id"] = new_user_id

            await update.message.reply_text(
                f"⚠️ *CONFIRM ADD USER*\n\n"
                f"You are about to add:\n\n"
                f"*Name:* {display_name}\n"
                f"*ID:* `{new_user_id}`\n"
                f"*Role:* VIEWER\n\n"
                f"Do you want to proceed?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Must be a number.")

    async def remove_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a user - with confirmation"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text(
                "❌ You don't have permission to remove users."
            )
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /remove [telegram_id]\n\n" "Example: /remove 123456789"
            )
            return

        try:
            target_user_id = int(context.args[0])

            # Can't remove super admins
            if target_user_id in SUPER_ADMIN_IDS:
                await update.message.reply_text("❌ Cannot remove super admins.")
                return

            # Get target user info
            target_user = db.get_user(target_user_id)
            if not target_user:
                await update.message.reply_text(f"❌ User {target_user_id} not found.")
                return

            # Store pending action and ask for confirmation
            buttons = [
                [InlineKeyboardButton("✅ Confirm Remove", callback_data=f"admin_remove_confirm_{target_user_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_remove_cancel")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)

            await update.message.reply_text(
                f"⚠️ *CONFIRM REMOVE USER*\n\n"
                f"You are about to remove:\n\n"
                f"*Name:* {target_user['display_name']}\n"
                f"*ID:* `{target_user_id}`\n"
                f"*Role:* {target_user['role'].upper()}\n\n"
                f"⚠️ This action cannot be undone.\n\n"
                f"Do you want to proceed?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def handle_admin_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin confirmation callbacks for add/remove/promote"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user = db.get_user(user_id)
        
        if not user or user["role"] not in ["admin", "superadmin"]:
            await query.edit_message_text("❌ You don't have permission for this action.")
            return
        
        callback_data = query.data
        
        # Handle ADD confirmations
        if callback_data.startswith("admin_add_confirm_"):
            new_user_id = int(callback_data.replace("admin_add_confirm_", ""))
            display_name = context.user_data.get("pending_add_name", "Unknown")
            
            # Check if user still doesn't exist
            existing = db.get_user(new_user_id)
            if existing:
                await query.edit_message_text(
                    f"❌ User {new_user_id} is already registered as {existing['role']}."
                )
                context.user_data.clear()
                return
            
            db.add_user(new_user_id, display_name, "viewer", user_id)
            
            await query.edit_message_text(
                f"✅ *USER ADDED*\n\n"
                f"*Name:* {display_name}\n"
                f"*ID:* `{new_user_id}`\n"
                f"*Role:* VIEWER\n\n"
                f"They can now use /start to access the bot.",
                parse_mode="Markdown",
            )
            context.user_data.clear()
        
        elif callback_data == "admin_add_cancel":
            await query.edit_message_text("👍 Add user cancelled.")
            context.user_data.clear()
        
        # Handle REMOVE confirmations
        elif callback_data.startswith("admin_remove_confirm_"):
            target_user_id = int(callback_data.replace("admin_remove_confirm_", ""))
            
            # Can't remove super admins
            if target_user_id in SUPER_ADMIN_IDS:
                await query.edit_message_text("❌ Cannot remove super admins.")
                return
            
            target_user = db.get_user(target_user_id)
            if target_user:
                db.remove_user(target_user_id)
                await query.edit_message_text(
                    f"✅ *USER REMOVED*\n\n"
                    f"*Name:* {target_user['display_name']}\n"
                    f"*ID:* `{target_user_id}`",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(f"❌ User {target_user_id} not found.")
        
        elif callback_data == "admin_remove_cancel":
            await query.edit_message_text("👍 Remove user cancelled.")
        
        # Handle PROMOTE confirmations
        elif callback_data.startswith("admin_promote_confirm_"):
            parts = callback_data.replace("admin_promote_confirm_", "").split("_")
            target_user_id = int(parts[0])
            new_role = parts[1]
            
            # Only superadmin can promote
            if user["role"] != "superadmin":
                await query.edit_message_text("❌ Only super admins can change user roles.")
                return
            
            target_user = db.get_user(target_user_id)
            if target_user:
                old_role = target_user['role']
                db.update_user_role(target_user_id, new_role)
                await query.edit_message_text(
                    f"✅ *ROLE CHANGED*\n\n"
                    f"*Name:* {target_user['display_name']}\n"
                    f"*ID:* `{target_user_id}`\n"
                    f"*Previous Role:* {old_role.upper()}\n"
                    f"*New Role:* {new_role.upper()}",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(f"❌ User {target_user_id} not found.")
            context.user_data.clear()
        
        elif callback_data == "admin_promote_cancel":
            await query.edit_message_text("👍 Role change cancelled.")
            context.user_data.clear()

    async def list_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all users"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text("❌ You don't have permission to list users.")
            return

        users = db.get_all_users()

        if not users:
            await update.message.reply_text("No users registered.")
            return

        # Group by role
        role_groups = {}
        for u in users:
            role = u["role"]
            if role not in role_groups:
                role_groups[role] = []
            role_groups[role].append(u)

        message = "👥 <b>REGISTERED USERS</b>\n\n"

        for role in ["superadmin", "admin", "relief_member", "student_admin", "viewer"]:
            if role in role_groups:
                message += f"<b>{role.upper()}:</b>\n"
                for u in role_groups[role]:
                    # Escape HTML special characters in display name
                    safe_name = str(u['display_name']).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    message += f"• {safe_name} ({u['telegram_id']})\n"
                message += "\n"

        await update.message.reply_text(message, parse_mode="HTML")

    async def promote_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Promote user to uploader or uploadadmin (superadmin only) - with confirmation"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] != "superadmin":
            await update.message.reply_text("❌ Only super admins can promote users.")
            return

        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /promote [user_id] [role]\n\n"
                "Roles: viewer, relief_member, admin\n"
                "Example: /promote 123456789 uploader"
            )
            return

        try:
            target_user_id = int(context.args[0])
            new_role = context.args[1].lower()

            if new_role not in ["viewer", "relief_member", "admin", "student_admin"]:
                await update.message.reply_text(
                    "❌ Invalid role. Use: viewer, uploader, or uploadadmin"
                )
                return

            target_user = db.get_user(target_user_id)
            if not target_user:
                await update.message.reply_text(
                    f"❌ User {target_user_id} is not registered."
                )
                return

            # Store pending action and ask for confirmation
            context.user_data["pending_promote_id"] = target_user_id
            context.user_data["pending_promote_role"] = new_role

            buttons = [
                [InlineKeyboardButton("✅ Confirm Change", callback_data=f"admin_promote_confirm_{target_user_id}_{new_role}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_promote_cancel")],
            ]
            keyboard = InlineKeyboardMarkup(buttons)

            await update.message.reply_text(
                f"⚠️ *CONFIRM ROLE CHANGE*\n\n"
                f"You are about to change:\n\n"
                f"*Name:* {target_user['display_name']}\n"
                f"*ID:* `{target_user_id}`\n"
                f"*Current Role:* {target_user['role'].upper()}\n"
                f"*New Role:* {new_role.upper()}\n\n"
                f"Do you want to proceed?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def generate_new_code(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Generate new daily code (superadmin only)"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] != "superadmin":
            await update.message.reply_text("❌ Only super admins can generate codes.")
            return

        new_code = db.generate_new_daily_code()

        await update.message.reply_text(
            f"🔐 *New Upload Code Generated:*\n\n`{new_code}`\n\n"
            f"Valid until midnight (SGT)",
            parse_mode="Markdown",
        )

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show usage statistics (superadmin only)"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] != "superadmin":
            await update.message.reply_text("❌ Only super admins can view stats.")
            return

        stats = db.get_stats()

        message = "📊 *BOT STATISTICS*\n\n"
        message += f"Total Users: {stats['total_users']}\n"
        message += f"• Super Admins: {stats['superadmins']}\n"
        message += f"• Admins: {stats.get('admin', 0)}\n"
        message += f"• Relief Members: {stats.get('relief_member', 0)}\n"
        message += f"• Student Admins: {stats.get('student_admin', 0)}\n"
        message += f"• Viewers: {stats['viewers']}\n\n"
        message += f"Today's Entries: {stats['today_entries']}"

        await update.message.reply_text(message, parse_mode="Markdown")

    async def manual_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually trigger data purge (superadmin only)"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] != "superadmin":
            await update.message.reply_text("❌ Only super admins can purge data.")
            return

        deleted_count = db.purge_old_data()

        await update.message.reply_text(
            f"🗑️ Purged {deleted_count} old entries.",
            parse_mode="Markdown",
        )

    async def my_uploads(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's uploads for today"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["admin", "superadmin"]:
            await update.message.reply_text("❌ You don't have upload permissions.")
            return

        entries = db.get_user_uploads_today(user_id)

        if not entries:
            await update.message.reply_text("You haven't uploaded anything today.")
            return

        message = "📤 *YOUR UPLOADS TODAY:*\n\n"

        for i, entry in enumerate(entries, 1):
            tag = entry["tag"]
            timestamp = entry["timestamp"]
            content_type = entry["content"]["type"]

            message += f"{i}. [{tag}] - {content_type} at {timestamp}\n"

        await update.message.reply_text(message, parse_mode="Markdown")

    # ============ SUPER ADMIN HIDDEN COMMANDS ============

    async def mass_upload_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start CSV mass upload process - super admin only"""
        user_id = update.effective_user.id
        
        if user_id not in SUPER_ADMIN_IDS:
            return  # Silently ignore
        
        await update.message.reply_text(
            "📤 *MASS USER UPLOAD*\n\n"
            "Send a CSV file with the following format:\n"
            "`telegram_id,name,role`\n\n"
            "Example:\n"
            "```\n"
            "123456789,John Teacher,viewer\n"
            "987654321,Jane Admin,admin\n"
            "111222333,Bob Relief,relief_member\n"
            "```\n\n"
            "⚠️ *Warning:* This will REPLACE all existing users except super admins.\n\n"
            "Valid roles: `viewer`, `relief_member`, `admin`\n\n"
            "Send /cancel to abort.",
            parse_mode="Markdown"
        )
        
        context.user_data["awaiting_csv"] = True
        return AWAITING_CONTENT

    async def process_csv_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process CSV file for mass user upload"""
        user_id = update.effective_user.id
        
        if user_id not in SUPER_ADMIN_IDS:
            return ConversationHandler.END
        
        if not update.message.document:
            await update.message.reply_text("❌ Please send a CSV file.")
            return AWAITING_CONTENT
        
        # Download the file
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()
        
        try:
            # Parse CSV
            csv_content = file_bytes.decode('utf-8')
            lines = csv_content.strip().split('\n')
            
            # Skip header if present
            if lines[0].lower().startswith('telegram_id') or lines[0].lower().startswith('id'):
                lines = lines[1:]
            
            new_users = []
            errors = []
            
            for i, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                    
                parts = line.strip().split(',')
                if len(parts) < 3:
                    errors.append(f"Line {i}: Invalid format")
                    continue
                
                try:
                    tid = int(parts[0].strip())
                    name = parts[1].strip()
                    role = parts[2].strip().lower()
                    
                    # Validate role
                    if role not in ['viewer', 'relief_member', 'admin', 'student_admin']:
                        errors.append(f"Line {i}: Invalid role '{role}'")
                        continue
                    
                    # Skip if it's a super admin from config
                    if tid in SUPER_ADMIN_IDS:
                        errors.append(f"Line {i}: Cannot modify super admin {tid}")
                        continue
                    
                    new_users.append({'telegram_id': tid, 'name': name, 'role': role})
                    
                except ValueError:
                    errors.append(f"Line {i}: Invalid telegram ID")
            
            if not new_users:
                await update.message.reply_text(
                    f"❌ No valid users found in CSV.\n\nErrors:\n" + "\n".join(errors[:10])
                )
                context.user_data.clear()
                return ConversationHandler.END
            
            # Delete all non-superadmin users
            db.delete_non_superadmin_users(SUPER_ADMIN_IDS)
            
            # Add new users
            added = 0
            for u in new_users:
                try:
                    db.add_user(u['telegram_id'], u['name'], u['role'], user_id)
                    added += 1
                except Exception as e:
                    errors.append(f"Failed to add {u['telegram_id']}: {e}")
            
            result_msg = f"✅ *Mass Upload Complete*\n\n"
            result_msg += f"Added: {added} users\n"
            
            if errors:
                result_msg += f"\n⚠️ Errors ({len(errors)}):\n"
                result_msg += "\n".join(errors[:5])
                if len(errors) > 5:
                    result_msg += f"\n... and {len(errors)-5} more"
            
            await update.message.reply_text(result_msg, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"CSV processing error: {e}")
            await update.message.reply_text(f"❌ Error processing CSV: {e}")
        
        context.user_data.clear()
        return ConversationHandler.END

    async def add_superadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a new super admin - hidden command"""
        user_id = update.effective_user.id
        
        if user_id not in SUPER_ADMIN_IDS:
            return  # Silently ignore
        
        if not context.args:
            await update.message.reply_text(
                "Usage: /addsuperadmin [telegram_id]\n\n"
                "Example: /addsuperadmin 123456789"
            )
            return
        
        try:
            new_admin_id = int(context.args[0])
            
            # Check if already exists
            existing = db.get_user(new_admin_id)
            if existing and existing['role'] == 'superadmin':
                await update.message.reply_text(f"❌ User {new_admin_id} is already a super admin.")
                return
            
            if existing:
                db.update_user_role(new_admin_id, 'superadmin')
            else:
                db.add_user(new_admin_id, f"SuperAdmin_{new_admin_id}", 'superadmin', user_id)
            
            await update.message.reply_text(
                f"✅ Added super admin: {new_admin_id}\n\n"
                f"⚠️ Note: Only super admins in the config file are fully protected."
            )
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def remove_superadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a super admin - hidden command"""
        user_id = update.effective_user.id
        
        if user_id not in SUPER_ADMIN_IDS:
            return  # Silently ignore
        
        if not context.args:
            await update.message.reply_text(
                "Usage: /removesuperadmin [telegram_id]\n\n"
                "Example: /removesuperadmin 123456789"
            )
            return
        
        try:
            target_id = int(context.args[0])
            
            # Cannot remove original super admins from config
            if target_id in SUPER_ADMIN_IDS:
                await update.message.reply_text(
                    f"❌ Cannot remove protected super admin {target_id}.\n"
                    f"This account is protected in the config file."
                )
                return
            
            target_user = db.get_user(target_id)
            if not target_user or target_user['role'] != 'superadmin':
                await update.message.reply_text(f"❌ User {target_id} is not a super admin.")
                return
            
            db.remove_user(target_id)
            await update.message.reply_text(f"✅ Removed super admin: {target_id}")
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def list_superadmins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all super admins - hidden command"""
        user_id = update.effective_user.id
        
        if user_id not in SUPER_ADMIN_IDS:
            return  # Silently ignore
        
        users = db.get_all_users()
        superadmins = [u for u in users if u['role'] == 'superadmin']
        
        message = "👑 *SUPER ADMINS*\n\n"
        
        for u in superadmins:
            protected = "🔒" if u['telegram_id'] in SUPER_ADMIN_IDS else ""
            message += f"• {u['display_name']} ({u['telegram_id']}) {protected}\n"
        
        message += "\n🔒 = Protected (in config file)"
        
        await update.message.reply_text(message, parse_mode="Markdown")

    async def daily_purge_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Daily job to purge old data"""
        logger.info("Running daily purge job...")

        deleted_count = db.purge_old_data()

        logger.info(f"Purged {deleted_count} entries.")

        # Notify super admins
        for admin_id in SUPER_ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔄 *Daily Reset Complete*\n\n"
                    f"Purged: {deleted_count} entries",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    def _is_todays_event_pdf(self, filename: str):
        """
        Check if filename matches dd_mm_yy_eventname.pdf or dd_mm_yyyy_eventname.pdf
        and the date in the filename matches today.
        Returns (is_match, event_name or None).
        """
        import re
        if not filename or not filename.lower().endswith('.pdf'):
            return False, None
        m = re.match(r'^(\d{2})_(\d{2})_(\d{2,4})_(.+)\.pdf$', filename, re.IGNORECASE)
        if not m:
            return False, None
        dd, mm, yy_or_yyyy, eventname = m.groups()
        try:
            day = int(dd)
            month = int(mm)
            if len(yy_or_yyyy) == 2:
                year = 2000 + int(yy_or_yyyy)
            else:
                year = int(yy_or_yyyy)
            today = get_singapore_now().date()
            if (day, month, year) == (today.day, today.month, today.year):
                return True, eventname.strip('_').replace('_', ' ')
            return False, None
        except (ValueError, TypeError):
            return False, None

    async def sync_folder_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Scheduled job to sync a single folder from Google Drive. Uses context.job.data['folder_name']."""
        if not self.drive_sync:
            return
        
        folder_name = context.job.data.get('folder_name') if context.job.data else None
        if not folder_name:
            logger.warning("sync_folder_job: no folder_name in job data")
            return
        
        sync_user_id = SUPER_ADMIN_IDS[0] if SUPER_ADMIN_IDS else None
        if not sync_user_id:
            return
        
        folder = db.get_folder_by_name(folder_name)
        if not folder:
            # Try to discover folder from Drive
            drive_folder = self.drive_sync.get_folder_by_name(folder_name)
            if drive_folder:
                db.add_or_update_drive_folder(
                    folder_name=folder_name,
                    drive_folder_id=drive_folder['id'],
                    parent_folder_id=GOOGLE_DRIVE_ROOT_FOLDER_ID
                )
                folder = db.get_folder_by_name(folder_name)
        
        if not folder:
            logger.warning(f"sync_folder_job: folder '{folder_name}' not found")
            return
        
        drive_folder_id = folder['drive_folder_id']
        logger.info(f"Running scheduled sync for folder: {folder_name}")
        
        try:
            files = self.drive_sync.list_files_in_folder(drive_folder_id)
            if not files:
                db.update_folder_sync_time(folder['id'])
                db.log_sync(folder_id=folder['id'], files_synced=0, files_processed=0, errors=None, synced_by=sync_user_id)
                return
            
            # Today's Event: only process PDFs with dd_mm_yy_eventname.pdf where date = today
            if folder_name == "Today's Event":
                today = get_singapore_now().date()
                filtered_files = []
                for f in files:
                    name = f.get('name', '')
                    is_match, event_name = self._is_todays_event_pdf(name)
                    if is_match:
                        f['_event_name'] = event_name
                        filtered_files.append(f)
                files = filtered_files
                logger.info(f"Today's Event: {len(filtered_files)} PDF(s) match today's date ({today})")
            
            files_processed_count = 0
            errors = []
            for file in files:
                try:
                    file_content = self.drive_sync.get_file_content(file)
                    if not file_content:
                        errors.append(f"{file['name']}: Failed to download")
                        continue
                    
                    category = self.drive_sync.detect_file_category(file['name'], folder_name)
                    extracted_text = ""
                    file_type = "document"
                    
                    if file.get('mimeType', '').startswith('image/'):
                        extracted_text = self.analyze_image(file_content, category)
                        file_type = "photo"
                    elif file.get('mimeType', '') == 'application/pdf' or file['name'].lower().endswith('.pdf'):
                        extracted_text = self.analyze_pdf(file_content, category)
                    elif file.get('mimeType', '').startswith('text/'):
                        try:
                            extracted_text = file_content.decode('utf-8')
                        except:
                            extracted_text = file_content.decode('latin-1')
                    elif file.get('mimeType', '') == 'application/vnd.google-apps.spreadsheet':
                        try:
                            extracted_text = file_content.decode('utf-8')
                        except:
                            extracted_text = file_content.decode('latin-1')
                    else:
                        if file_content[:4] == b'%PDF':
                            extracted_text = self.analyze_pdf(file_content, category)
                        else:
                            try:
                                extracted_text = file_content.decode('utf-8')
                            except:
                                extracted_text = f"[Binary file: {file['name']}]"
                    
                    content_data = {
                        "type": file_type,
                        "file_name": file['name'],
                        "extracted_text": extracted_text,
                        "source": "google_drive_scheduled",
                        "folder": folder_name,
                        "drive_folder_id": drive_folder_id,
                        "drive_file_id": file.get('id'),
                    }
                    if folder_name == "Today's Event" and file.get('_event_name'):
                        content_data["event_name"] = file['_event_name']
                    if content_data.get("drive_file_id"):
                        db.add_or_update_drive_entry(sync_user_id, category, content_data)
                    else:
                        db.add_entry(sync_user_id, category, content_data)
                    files_processed_count += 1
                except Exception as e:
                    logger.error(f"Error processing file {file['name']}: {e}")
                    errors.append(f"{file['name']}: {str(e)}")
            
            db.update_folder_sync_time(folder['id'])
            error_str = "; ".join(errors[-10:]) if errors else None
            db.log_sync(
                folder_id=folder['id'],
                files_synced=len(files),
                files_processed=files_processed_count,
                errors=error_str,
                synced_by=sync_user_id
            )
            logger.info(f"Scheduled sync complete for {folder_name}: {files_processed_count}/{len(files)} files")
        except Exception as e:
            logger.error(f"Error syncing folder {folder_name}: {e}", exc_info=True)

    def setup_handlers(self):
        """Setup all command and message handlers"""

        # Upload conversation handler with menu, privacy warning, and relief activation
        upload_conv = ConversationHandler(
            entry_points=[CommandHandler("upload", self.upload_start)],
            states={
                UPLOAD_MENU: [
                    CallbackQueryHandler(self.handle_upload_menu, pattern="^upload_|^confirm_|^cancel_"),
                    MessageHandler(filters.ALL, self.handle_upload_menu_message),
                ],
                PRIVACY_WARNING: [
                    CallbackQueryHandler(self.handle_privacy_warning, pattern="^privacy_"),
                    MessageHandler(filters.ALL, self.handle_privacy_warning_message),
                ],
                SELECTING_TAG: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.tag_selected)
                ],
                AWAITING_CONTENT: [
                    MessageHandler(
                        filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                        self.content_received,
                    )
                ],
                SELECTING_UPLOAD_TO_DELETE: [
                    CallbackQueryHandler(self.handle_delete_entry, pattern="^delete_|^confirm_delete|^cancel_delete"),
                    MessageHandler(filters.ALL, self.handle_delete_menu_message),
                ],
                RELIEF_ACTIVATION: [
                    CallbackQueryHandler(self.handle_relief_activation, pattern="^relief_"),
                ],
                SELECTING_RELIEF_REMINDERS: [
                    CallbackQueryHandler(self.handle_relief_individual_selection, pattern="^relief_"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel_upload),
                CallbackQueryHandler(self.cancel_upload_callback, pattern="^cancel$"),
            ],
            allow_reentry=True,
            name="upload_conversation",
        )

        # Mass upload conversation handler (super admin only)
        mass_upload_conv = ConversationHandler(
            entry_points=[CommandHandler("massupload", self.mass_upload_start)],
            states={
                AWAITING_CONTENT: [
                    MessageHandler(filters.Document.ALL, self.process_csv_upload)
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_upload)],
        )

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("helprelief", self.helprelief))
        self.app.add_handler(CommandHandler("helpadmin", self.helpadmin))
        self.app.add_handler(CommandHandler("helpsuper", self.helpsuper))
        self.app.add_handler(upload_conv)
        self.app.add_handler(mass_upload_conv)
        self.app.add_handler(CommandHandler("ask", self.ask_query))
        self.app.add_handler(CommandHandler("today", self.today_summary))
        self.app.add_handler(CommandHandler("myuploads", self.my_uploads))
        self.app.add_handler(CommandHandler("add", self.add_user))
        self.app.add_handler(CommandHandler("remove", self.remove_user))
        self.app.add_handler(CommandHandler("list", self.list_users))
        self.app.add_handler(CommandHandler("promote", self.promote_user))
        self.app.add_handler(CommandHandler("stats", self.show_stats))
        self.app.add_handler(CommandHandler("purge", self.manual_purge))
        # Relief management commands
        self.app.add_handler(CommandHandler("reliefstatus", self.relief_status))
        self.app.add_handler(CommandHandler("cancelrelief", self.cancel_relief))
        # Google Drive sync commands
        self.app.add_handler(CommandHandler("setfolder", self.set_folder))
        self.app.add_handler(CommandHandler("listfolders", self.list_folders))
        self.app.add_handler(CommandHandler("sync", self.sync_drive))
        self.app.add_handler(CommandHandler("drivefolder", self.drive_folder_info))
        self.app.add_handler(CommandHandler("syncstatus", self.sync_status))
        self.app.add_handler(CommandHandler("assume", self.assume_role))
        self.app.add_handler(CommandHandler("resume", self.resume_role))
        # Hidden super admin commands
        self.app.add_handler(CommandHandler("addsuperadmin", self.add_superadmin))
        self.app.add_handler(CommandHandler("removesuperadmin", self.remove_superadmin))
        self.app.add_handler(CommandHandler("listsuperadmins", self.list_superadmins))
        
        # Callback query handler for summary buttons
        self.app.add_handler(CallbackQueryHandler(self.handle_summary_callback, pattern="^summary_"))
        
        # Callback query handler for admin confirmations (add/remove/promote)
        self.app.add_handler(CallbackQueryHandler(self.handle_admin_callback, pattern="^admin_"))
        
        # Callback query handler for relief command buttons
        self.app.add_handler(CallbackQueryHandler(self.handle_relief_command_callback, pattern="^relief_cmd_"))
        
        # Catch-all callback handler for debugging (should not normally be triggered)
        self.app.add_handler(CallbackQueryHandler(self.handle_unknown_callback))

    async def handle_unknown_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callbacks that weren't caught by other handlers - for debugging"""
        query = update.callback_query
        await query.answer()
        logger.warning(f"Unhandled callback: {query.data} from user {query.from_user.id}")
        # Try to handle common upload callbacks that might have been missed
        if query.data.startswith("upload_") or query.data.startswith("privacy_"):
            await query.edit_message_text(
                "⚠️ Session expired. Please use /upload to start again."
            )

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Log errors"""
        error = context.error
        logger.error(f"Exception while handling an update: {error}", exc_info=error)
        logger.error(f"Update that caused error: {update}")
        
        # Try to send error message to user if possible
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ An error occurred. Please try again or contact an admin."
                )
            except:
                pass

    def run(self):
        """Start the bot"""
        self.app = Application.builder().token(TELEGRAM_TOKEN).build()

        # Setup handlers
        self.setup_handlers()
        
        # Add error handler
        self.app.add_error_handler(self.error_handler)

        # Schedule daily purge at 11 PM
        job_queue = self.app.job_queue
        job_queue.run_daily(
            self.daily_purge_job,
            time=time(hour=23, minute=0),  # 11 PM
            name="daily_purge",
        )

        # Schedule relief reminder check every minute (during school hours: 7am-5pm)
        job_queue.run_repeating(
            self.relief_reminder_job,
            interval=60,  # Every 60 seconds
            first=10,  # Start after 10 seconds
            name="relief_reminders",
        )

        # Schedule per-folder Drive sync (Relief Committee 6pm, Relief Timetable/Weekly Bulletin 7:45am)
        # Student Movement is uploaded via Telegram only - no Drive sync
        if self.drive_sync:
            for folder_name, (hour, minute) in SYNC_SCHEDULE.items():
                job_queue.run_daily(
                    self.sync_folder_job,
                    time=time(hour=hour, minute=minute),
                    name=f"sync_{folder_name.replace(' ', '_')}",
                    data={"folder_name": folder_name},
                )
                logger.info(f"Drive sync scheduled for {folder_name} at {hour:02d}:{minute:02d} SGT")

        logger.info("Bot started successfully!")

        # Start polling
        # Add error handling for network issues (use time_module to avoid shadowing datetime.time)
        import time as time_module
        max_retries = 3
        retry_delay = 10
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Starting Telegram bot polling (attempt {attempt + 1}/{max_retries})...")
                self.app.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,  # Drop pending updates on restart
                    close_loop=False  # Don't close event loop on error
                )
                # If we get here, polling stopped normally
                break
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error in polling (attempt {attempt + 1}/{max_retries}): {error_msg}", exc_info=True)
                
                # Check for specific error types
                if "Connection" in error_msg or "Network" in error_msg or "timeout" in error_msg.lower():
                    logger.warning("Network-related error detected. This might be temporary.")
                elif "Unauthorized" in error_msg or "401" in error_msg:
                    logger.error("Bot token is invalid or expired. Check TELEGRAM_TOKEN environment variable.")
                    raise  # Don't retry for auth errors
                elif "Too Many Requests" in error_msg or "429" in error_msg:
                    logger.warning("Rate limited by Telegram API. Waiting longer before retry...")
                    retry_delay = 60  # Wait 60 seconds for rate limits
                
                if attempt < max_retries - 1:
                    logger.info(f"Waiting {retry_delay} seconds before retry...")
                    time_module.sleep(retry_delay)
                else:
                    logger.error("Max retries reached. Bot polling failed.")
                    raise


if __name__ == "__main__":
    bot = SchoolAdminBot()
    bot.run()
