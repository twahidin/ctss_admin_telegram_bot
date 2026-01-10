import os
import logging
from datetime import datetime, time
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
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

# Initialize database
db = Database()

# Initialize Claude client
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


class SchoolAdminBot:
    def __init__(self):
        self.app = None

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
        """Show help based on user role"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user:
            await update.message.reply_text(
                "You're not registered. Use /start to begin."
            )
            return

        role = user["role"]

        help_text = "📚 *AVAILABLE COMMANDS*\n\n"

        # Viewer commands (everyone)
        help_text += "*For Everyone:*\n"
        help_text += "/ask [question] - Query today's information\n"
        help_text += "/today - See all categories and entry counts\n"
        help_text += "/help - Show this help message\n\n"

        # Uploader commands
        if role in ["uploader", "uploadadmin", "superadmin"]:
            help_text += "*For Uploaders:*\n"
            help_text += "/upload - Upload new information\n"
            help_text += "/myuploads - See your uploads today\n"
            help_text += "/code - Get today's upload code\n\n"

        # Upload admin commands
        if role in ["uploadadmin", "superadmin"]:
            help_text += "*For Upload Admins:*\n"
            help_text += "/add [user_id] - Add viewer\n"
            help_text += "/remove [user_id] - Remove user\n"
            help_text += "/list - Show all users\n\n"

        # Super admin commands
        if role == "superadmin":
            help_text += "*For Super Admins:*\n"
            help_text += "/promote [user_id] [role] - Promote user\n"
            help_text += "/newcode - Generate new upload code\n"
            help_text += "/stats - Show usage statistics\n"
            help_text += "/purge - Manually purge old data\n"

        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def upload_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start upload process"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["uploader", "uploadadmin", "superadmin"]:
            await update.message.reply_text("❌ You don't have upload permissions.")
            return ConversationHandler.END

        # Show tag selection
        tag_buttons = [[f"{i+1}️⃣ {tag}"] for i, tag in enumerate(TAGS)]
        reply_markup = ReplyKeyboardMarkup(tag_buttons, one_time_keyboard=True)

        await update.message.reply_text(
            "📤 *UPLOAD INFORMATION*\n\n"
            "Select a category by sending the number:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

        return SELECTING_TAG

    async def tag_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tag selection"""
        try:
            tag_number = int(update.message.text.split("️⃣")[0]) - 1
            if 0 <= tag_number < len(TAGS):
                selected_tag = TAGS[tag_number]
                context.user_data["selected_tag"] = selected_tag

                await update.message.reply_text(
                    f"Category: *{selected_tag}*\n\n"
                    f"Now send:\n"
                    f"• A photo/image\n"
                    f"• A PDF document\n"
                    f"• Or type your message",
                    parse_mode="Markdown",
                    reply_markup=ReplyKeyboardRemove(),
                )

                return AWAITING_CONTENT
        except (ValueError, IndexError):
            pass

        await update.message.reply_text("❌ Invalid selection. Please choose 1-6.")
        return SELECTING_TAG

    async def content_received(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle content upload (text, photo, or document)"""
        user_id = update.effective_user.id
        selected_tag = context.user_data.get("selected_tag")

        # Ask for upload code
        await update.message.reply_text(
            "🔐 Please enter today's upload code:\n\n"
            "(Ask an admin if you don't have it)"
        )

        # Store the content temporarily
        if update.message.photo:
            context.user_data["content_type"] = "photo"
            context.user_data["file_id"] = update.message.photo[-1].file_id
            context.user_data["caption"] = update.message.caption or ""
        elif update.message.document:
            context.user_data["content_type"] = "document"
            context.user_data["file_id"] = update.message.document.file_id
            context.user_data["caption"] = update.message.caption or ""
        else:
            context.user_data["content_type"] = "text"
            context.user_data["text_content"] = update.message.text

        return AWAITING_CODE

    async def verify_code_and_save(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Verify upload code and save content"""
        user_id = update.effective_user.id
        code_input = update.message.text.strip().upper()

        # Get today's code
        today_code = db.get_daily_code()

        if code_input != today_code:
            await update.message.reply_text(
                "❌ Invalid code. Upload cancelled.\n\n" "Use /upload to try again."
            )
            context.user_data.clear()
            return ConversationHandler.END

        # Code is valid, save the content
        selected_tag = context.user_data.get("selected_tag")
        content_type = context.user_data.get("content_type")

        content_data = {}

        if content_type == "photo":
            file_id = context.user_data.get("file_id")
            caption = context.user_data.get("caption")

            # Download file
            file = await context.bot.get_file(file_id)
            file_path = db.save_file(file_id, "photo", await file.download_as_bytearray())

            content_data = {
                "type": "photo",
                "file_path": file_path,
                "caption": caption,
            }

        elif content_type == "document":
            file_id = context.user_data.get("file_id")
            caption = context.user_data.get("caption")

            file = await context.bot.get_file(file_id)
            file_path = db.save_file(
                file_id, "document", await file.download_as_bytearray()
            )

            content_data = {
                "type": "document",
                "file_path": file_path,
                "caption": caption,
            }

        else:  # text
            text_content = context.user_data.get("text_content")
            content_data = {"type": "text", "content": text_content}

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
                model="claude-haiku-4-20250514",
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
                entry_text += f"[{content_data['type'].upper()}]"
                if caption:
                    entry_text += f" Caption: {caption}"

            context_parts.append(entry_text)

        return "\n\n".join(context_parts)

    async def today_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's entry counts by category"""
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
        for tag in TAGS:
            count = tag_counts.get(tag, 0)
            emoji = "✅" if count > 0 else "⚪️"
            summary += f"{emoji} {tag}: {count} entries\n"

        summary += f"\n*Total: {len(entries)} entries*"

        await update.message.reply_text(summary, parse_mode="Markdown")

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
        """Add a new viewer"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] not in ["uploadadmin", "superadmin"]:
            await update.message.reply_text("❌ You don't have permission to add users.")
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /add [telegram_id]\n\n" "Example: /add 123456789"
            )
            return

        try:
            new_user_id = int(context.args[0])

            # Check if user already exists
            existing = db.get_user(new_user_id)
            if existing:
                await update.message.reply_text(
                    f"❌ User {new_user_id} is already registered as {existing['role']}."
                )
                return

            # Add as viewer
            db.add_user(new_user_id, f"User_{new_user_id}", "viewer", user_id)

            await update.message.reply_text(
                f"✅ Added user {new_user_id} as VIEWER.\n\n"
                f"They can now use /start to access the bot."
            )

        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Must be a number.")

    async def remove_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a user"""
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

            db.remove_user(target_user_id)
            await update.message.reply_text(f"✅ Removed user {target_user_id}.")

        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

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

        message = "👥 *REGISTERED USERS*\n\n"

        for role in ["superadmin", "uploadadmin", "uploader", "viewer"]:
            if role in role_groups:
                message += f"*{role.upper()}:*\n"
                for u in role_groups[role]:
                    message += f"• {u['display_name']} ({u['telegram_id']})\n"
                message += "\n"

        await update.message.reply_text(message, parse_mode="Markdown")

    async def promote_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Promote user to uploader or uploadadmin (superadmin only)"""
        user_id = update.effective_user.id
        user = db.get_user(user_id)

        if not user or user["role"] != "superadmin":
            await update.message.reply_text("❌ Only super admins can promote users.")
            return

        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /promote [user_id] [role]\n\n"
                "Roles: uploader, uploadadmin\n"
                "Example: /promote 123456789 uploader"
            )
            return

        try:
            target_user_id = int(context.args[0])
            new_role = context.args[1].lower()

            if new_role not in ["uploader", "uploadadmin"]:
                await update.message.reply_text(
                    "❌ Invalid role. Use: uploader or uploadadmin"
                )
                return

            target_user = db.get_user(target_user_id)
            if not target_user:
                await update.message.reply_text(
                    f"❌ User {target_user_id} is not registered."
                )
                return

            db.update_user_role(target_user_id, new_role)

            await update.message.reply_text(
                f"✅ Promoted user {target_user_id} to {new_role.upper()}."
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
        message += f"Today's Entries: {stats['today_entries']}\n"
        message += f"Current Code: `{db.get_daily_code()}`"

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
            f"🗑️ Purged {deleted_count} old entries.\n\n"
            f"New daily code generated: `{db.get_daily_code()}`",
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

    async def daily_purge_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Daily job to purge old data and generate new code"""
        logger.info("Running daily purge job...")

        deleted_count = db.purge_old_data()
        new_code = db.get_daily_code()

        logger.info(f"Purged {deleted_count} entries. New code: {new_code}")

        # Notify super admins
        for admin_id in SUPER_ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔄 *Daily Reset Complete*\n\n"
                    f"Purged: {deleted_count} entries\n"
                    f"New upload code: `{new_code}`",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    def setup_handlers(self):
        """Setup all command and message handlers"""

        # Upload conversation handler
        upload_conv = ConversationHandler(
            entry_points=[CommandHandler("upload", self.upload_start)],
            states={
                SELECTING_TAG: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.tag_selected)
                ],
                AWAITING_CONTENT: [
                    MessageHandler(
                        filters.PHOTO | filters.Document.ALL | filters.TEXT,
                        self.content_received,
                    )
                ],
                AWAITING_CODE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, self.verify_code_and_save
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_upload)],
        )

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(upload_conv)
        self.app.add_handler(CommandHandler("ask", self.ask_query))
        self.app.add_handler(CommandHandler("today", self.today_summary))
        self.app.add_handler(CommandHandler("code", self.get_upload_code))
        self.app.add_handler(CommandHandler("myuploads", self.my_uploads))
        self.app.add_handler(CommandHandler("add", self.add_user))
        self.app.add_handler(CommandHandler("remove", self.remove_user))
        self.app.add_handler(CommandHandler("list", self.list_users))
        self.app.add_handler(CommandHandler("promote", self.promote_user))
        self.app.add_handler(CommandHandler("newcode", self.generate_new_code))
        self.app.add_handler(CommandHandler("stats", self.show_stats))
        self.app.add_handler(CommandHandler("purge", self.manual_purge))

    def run(self):
        """Start the bot"""
        self.app = Application.builder().token(TELEGRAM_TOKEN).build()

        # Setup handlers
        self.setup_handlers()

        # Schedule daily purge at midnight SGT (UTC+8)
        job_queue = self.app.job_queue
        job_queue.run_daily(
            self.daily_purge_job,
            time=time(hour=0, minute=0),  # Midnight
            name="daily_purge",
        )

        logger.info("Bot started successfully!")

        # Start polling
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    bot = SchoolAdminBot()
    bot.run()
