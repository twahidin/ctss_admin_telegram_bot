import os
import io
import base64
import logging
from datetime import datetime, time
import fitz  # PyMuPDF for PDF processing
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
from config import (
    TELEGRAM_TOKEN,
    CLAUDE_API_KEY,
    TAGS,
    SUPER_ADMIN_IDS,
    DAILY_CODE_LENGTH,
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

# Initialize database
db = Database()

# Initialize Claude client
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


class SchoolAdminBot:
    def __init__(self):
        self.app = None

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
                max_tokens=2000,
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
                                "text": f"""This is an image uploaded to the "{category}" category in a school admin system.
                                
Please extract ALL text and information from this image. Include:
- Names of teachers, staff, or students
- Class names (like 3A, 4B, etc.)
- Times and schedules
- Room numbers
- Any other relevant details

Format the extracted information clearly. If it's a schedule or table, preserve the structure.
If you can't read something clearly, note what you can see."""
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
        """Analyze PDF by converting pages to images and extracting text"""
        try:
            # Open PDF from bytes
            pdf_document = fitz.open(stream=pdf_data, filetype="pdf")
            all_extracted_text = []
            
            # Process each page (limit to first 10 pages to avoid too much processing)
            max_pages = min(len(pdf_document), 10)
            
            for page_num in range(max_pages):
                page = pdf_document[page_num]
                
                # Convert page to image (higher resolution for better OCR)
                mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better quality
                pix = page.get_pixmap(matrix=mat)
                
                # Convert to PNG bytes
                img_bytes = pix.tobytes("png")
                
                # Analyze this page image
                page_text = self.analyze_image(img_bytes, category)
                all_extracted_text.append(f"--- Page {page_num + 1} ---\n{page_text}")
            
            pdf_document.close()
            
            combined_text = "\n\n".join(all_extracted_text)
            logger.info(f"Extracted text from PDF ({max_pages} pages): {combined_text[:200]}...")
            return combined_text
            
        except Exception as e:
            logger.error(f"PDF analysis error: {e}")
            return f"[PDF analysis failed: {str(e)}]"

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

        help_text = "📚 *AVAILABLE COMMANDS*\n\n"

        # Basic commands for everyone
        help_text += "/ask [question] - Query today's information\n"
        help_text += "/today - See all categories and entry counts\n"
        help_text += "/help - Show this help message\n"

        # Uploader commands
        if role in ["uploader", "uploadadmin", "superadmin"]:
            help_text += "\n*Uploader Commands:*\n"
            help_text += "/upload - Upload new information\n"
            help_text += "/myuploads - See your uploads today\n"

        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def admin_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show admin help - for upload admins"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
            await update.message.reply_text("❌ This command is for admins only.")
            return

        help_text = "🔧 *ADMIN COMMANDS*\n\n"
        help_text += "/add [user_id] [name] - Add a new viewer\n"
        help_text += "  └ Example: /add 123456789 John Teacher\n"
        help_text += "/remove [user_id] - Remove a user\n"
        help_text += "/promote [user_id] [role] - Change user role\n"
        help_text += "  └ Roles: viewer, uploader, uploadadmin\n"
        help_text += "/list - Show all registered users\n"

        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def super_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show super admin help - hidden command"""
        user_id = update.effective_user.id
        
        # Only original super admins from config can see this
        if user_id not in SUPER_ADMIN_IDS:
            return  # Silently ignore

        help_text = "👑 *SUPER ADMIN COMMANDS*\n\n"
        help_text += "*User Management:*\n"
        help_text += "/massupload - Upload CSV to replace all users\n"
        help_text += "  └ CSV format: telegram\\_id,name,role\n"
        help_text += "/addsuperadmin [user_id] - Add a super admin\n"
        help_text += "/removesuperadmin [user_id] - Remove a super admin\n"
        help_text += "/listsuperadmins - List all super admins\n\n"
        help_text += "*System:*\n"
        help_text += "/stats - Show usage statistics\n"
        help_text += "/purge - Manually purge old data\n\n"
        help_text += "⚠️ Your account (ID: {}) is protected and cannot be removed.".format(user_id)

        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def upload_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start upload process - show initial menu"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["uploader", "uploadadmin", "superadmin"]:
            await update.message.reply_text("❌ You don't have upload permissions.")
            return ConversationHandler.END

        # Check user's uploads count today
        user_uploads = db.get_user_uploads_today(user_id)
        upload_count = len(user_uploads)

        # Build menu buttons
        buttons = [
            [InlineKeyboardButton("📤 Upload New Information", callback_data="upload_new")],
        ]
        
        # Only show remove options if user has uploads
        if upload_count > 0:
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
            # Proceed to tag selection
            tag_buttons = [[f"{i+1}️⃣ {tag}"] for i, tag in enumerate(TAGS)]
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
                deleted = db.delete_entry_by_id(entry_id, user_id)
                if deleted:
                    await query.edit_message_text("✅ Upload deleted successfully.")
                else:
                    await query.edit_message_text("❌ Could not delete upload. It may have already been removed.")
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
            tag_number = int(text.split("️⃣")[0]) - 1
            if 0 <= tag_number < len(TAGS):
                selected_tag = TAGS[tag_number]
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

        await update.message.reply_text("❌ Invalid selection. Please choose 1-6 or Cancel.")
        return SELECTING_TAG

    async def content_received(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle content upload (text, photo, or document) and save directly"""
        user_id = update.effective_user.id
        selected_tag = context.user_data.get("selected_tag")

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
                await update.message.reply_text("🔍 Analyzing PDF content...")
                extracted_text = self.analyze_pdf(bytes(doc_bytes), selected_tag)
            # Check if it's an image document
            elif file_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                await update.message.reply_text("🔍 Analyzing image content...")
                extracted_text = self.analyze_image(bytes(doc_bytes), selected_tag)

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

        # Save to database
        db.add_entry(user_id, selected_tag, content_data)

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
        entries = db.get_today_entries()

        if not entries:
            await update.message.reply_text(
                "📭 No information has been uploaded for today yet."
            )
            return

        # Build context for Claude
        context_text = self._build_context_for_claude(entries, query)

        # Query Claude
        try:
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[
                    {
                        "role": "user",
                        "content": f"""You are a helpful school admin assistant. Based on today's information, answer the following question concisely.

TODAY'S INFORMATION:
{context_text}

QUESTION: {query}

Provide a direct, concise answer. If the information isn't available, say so clearly.""",
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

    async def today_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's entry counts by category with option to view summary"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user:
            await update.message.reply_text("❌ Not registered. Use /start first.")
            return

        entries = db.get_today_entries()

        if not entries:
            await update.message.reply_text("📭 No information uploaded today.")
            return

        # Count by tag
        tag_counts = {}
        for entry in entries:
            tag = entry["tag"]
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

        summary = "📊 *TODAY'S INFORMATION*\n\n"
        
        # Build category list and buttons for categories with entries
        buttons = []
        for tag in TAGS:
            count = tag_counts.get(tag, 0)
            emoji = "✅" if count > 0 else "⚪️"
            summary += f"{emoji} {tag}: {count} entries\n"
            
            # Add button for categories that have entries
            if count > 0:
                buttons.append([InlineKeyboardButton(f"📋 {tag} Summary", callback_data=f"summary_{tag}")])

        summary += f"\n*Total: {len(entries)} entries*"
        
        # Add "All Categories" summary button if there are entries
        buttons.append([InlineKeyboardButton("📝 Full Summary (All Categories)", callback_data="summary_ALL")])
        
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)

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
        
        # Get entries
        entries = db.get_today_entries()
        
        if category != "ALL":
            entries = [e for e in entries if e["tag"] == category]
        
        if not entries:
            await query.edit_message_text(f"📭 No entries found for {category}.")
            return
        
        # Build context for Claude
        context_text = self._build_context_for_claude(entries, "summary")
        
        # Generate summary with Claude
        try:
            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Based on the following school information entries, provide a clear and organized summary of the MAIN POINTS.

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
            if category == "ALL":
                header = "📝 *FULL SUMMARY - ALL CATEGORIES*\n\n"
            else:
                header = f"📋 *SUMMARY - {category}*\n\n"
            
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

        if not user or user["role"] not in ["uploader", "uploadadmin", "superadmin"]:
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

        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
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

        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
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
        
        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
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

        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
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

        for role in ["superadmin", "uploadadmin", "uploader", "viewer"]:
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
                "Roles: viewer, uploader, uploadadmin\n"
                "Example: /promote 123456789 uploader"
            )
            return

        try:
            target_user_id = int(context.args[0])
            new_role = context.args[1].lower()

            if new_role not in ["viewer", "uploader", "uploadadmin"]:
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
        message += f"• Upload Admins: {stats['uploadadmins']}\n"
        message += f"• Uploaders: {stats['uploaders']}\n"
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

        if not user or user["role"] not in ["uploader", "uploadadmin", "superadmin"]:
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
            "123456789,John Teacher,uploader\n"
            "987654321,Jane Admin,uploadadmin\n"
            "111222333,Bob Viewer,viewer\n"
            "```\n\n"
            "⚠️ *Warning:* This will REPLACE all existing users except super admins.\n\n"
            "Valid roles: `viewer`, `uploader`, `uploadadmin`\n\n"
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
                    if role not in ['viewer', 'uploader', 'uploadadmin']:
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

    def setup_handlers(self):
        """Setup all command and message handlers"""

        # Upload conversation handler with menu and privacy warning
        upload_conv = ConversationHandler(
            entry_points=[CommandHandler("upload", self.upload_start)],
            states={
                UPLOAD_MENU: [
                    CallbackQueryHandler(self.handle_upload_menu, pattern="^upload_|^confirm_|^cancel_"),
                ],
                PRIVACY_WARNING: [
                    CallbackQueryHandler(self.handle_privacy_warning, pattern="^privacy_"),
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
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_upload)],
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
        self.app.add_handler(CommandHandler("adminhelp", self.admin_help))
        self.app.add_handler(CommandHandler("superhelp", self.super_help))
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
        # Hidden super admin commands
        self.app.add_handler(CommandHandler("addsuperadmin", self.add_superadmin))
        self.app.add_handler(CommandHandler("removesuperadmin", self.remove_superadmin))
        self.app.add_handler(CommandHandler("listsuperadmins", self.list_superadmins))
        
        # Callback query handler for summary buttons
        self.app.add_handler(CallbackQueryHandler(self.handle_summary_callback, pattern="^summary_"))
        
        # Callback query handler for admin confirmations (add/remove/promote)
        self.app.add_handler(CallbackQueryHandler(self.handle_admin_callback, pattern="^admin_"))

    def run(self):
        """Start the bot"""
        self.app = Application.builder().token(TELEGRAM_TOKEN).build()

        # Setup handlers
        self.setup_handlers()

        # Schedule daily purge at 11 PM
        job_queue = self.app.job_queue
        job_queue.run_daily(
            self.daily_purge_job,
            time=time(hour=23, minute=0),  # 11 PM
            name="daily_purge",
        )

        logger.info("Bot started successfully!")

        # Start polling
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    bot = SchoolAdminBot()
    bot.run()
