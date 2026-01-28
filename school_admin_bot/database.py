import os
import json
import random
import string
from datetime import datetime, date
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL, STORAGE_PATH, DAILY_CODE_LENGTH

ANIMALS = [
    "LION",
    "TIGER",
    "BEAR",
    "EAGLE",
    "SHARK",
    "WOLF",
    "PANDA",
    "HAWK",
    "DRAGON",
    "PHOENIX",
]


class Database:
    def __init__(self):
        self.db_url = DATABASE_URL
        self.storage_path = Path(STORAGE_PATH)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.init_database()

    def get_connection(self):
        """Get database connection"""
        return psycopg.connect(self.db_url)

    def init_database(self):
        """Initialize database tables"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Users table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                added_by BIGINT,
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Daily entries table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_entries (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                tag TEXT NOT NULL,
                content JSONB NOT NULL,
                uploaded_by BIGINT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (uploaded_by) REFERENCES users(telegram_id)
            )
        """
        )

        # Create index on date for fast queries
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_entries_date 
            ON daily_entries(date)
        """
        )

        # Daily codes table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_codes (
                date DATE PRIMARY KEY,
                code TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Relief reminders table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS relief_reminders (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                teacher_name TEXT NOT NULL,
                teacher_telegram_id BIGINT,
                relief_time TIME NOT NULL,
                period TEXT,
                class_info TEXT,
                room TEXT,
                original_teacher TEXT,
                reminder_sent BOOLEAN DEFAULT FALSE,
                activated BOOLEAN DEFAULT FALSE,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Create index on date for relief reminders
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_relief_reminders_date 
            ON relief_reminders(date)
        """
        )

        # No-show reports table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS noshow_reports (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                relief_reminder_id INTEGER REFERENCES relief_reminders(id),
                teacher_name TEXT NOT NULL,
                reported_by BIGINT NOT NULL,
                reporter_name TEXT,
                situation TEXT NOT NULL,
                reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Google Drive folders table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS drive_folders (
                id SERIAL PRIMARY KEY,
                folder_name TEXT NOT NULL UNIQUE,
                drive_folder_id TEXT NOT NULL UNIQUE,
                parent_folder_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_synced_at TIMESTAMP
            )
        """
        )

        # Folder-role access mapping (many-to-many)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS folder_role_access (
                id SERIAL PRIMARY KEY,
                folder_id INTEGER REFERENCES drive_folders(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(folder_id, role)
            )
        """
        )

        # User-folder access overrides (optional, for individual exceptions)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_folder_access (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                folder_id INTEGER REFERENCES drive_folders(id) ON DELETE CASCADE,
                granted BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, folder_id),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """
        )

        # Drive sync log
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS drive_sync_log (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                folder_id INTEGER REFERENCES drive_folders(id),
                files_synced INTEGER DEFAULT 0,
                files_processed INTEGER DEFAULT 0,
                errors TEXT,
                synced_by BIGINT,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Webhook channels and page tokens
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS drive_webhooks (
                id SERIAL PRIMARY KEY,
                folder_id TEXT NOT NULL,
                channel_id TEXT NOT NULL UNIQUE,
                resource_id TEXT,
                webhook_url TEXT NOT NULL,
                page_token TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        """
        )

        # Track shortcuts and their target files for watching
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS shortcut_targets (
                id SERIAL PRIMARY KEY,
                shortcut_id TEXT NOT NULL,
                shortcut_name TEXT NOT NULL,
                target_file_id TEXT NOT NULL,
                target_file_name TEXT,
                watched_folder_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(shortcut_id, target_file_id)
            )
        """
        )

        # Role assumption for superadmins (testing feature)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS role_assumptions (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL UNIQUE,
                original_role TEXT NOT NULL,
                assumed_role TEXT NOT NULL,
                assumed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """
        )

        # Create indexes
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_folder_role_access_folder 
            ON folder_role_access(folder_id)
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_folder_access_user 
            ON user_folder_access(telegram_id)
        """
        )

        conn.commit()
        cursor.close()
        conn.close()

        # Ensure today's code exists
        self.get_daily_code()

    # ===== USER MANAGEMENT =====

    def add_user(self, telegram_id, display_name, role, added_by):
        """Add a new user"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO users (telegram_id, display_name, role, added_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO NOTHING
        """,
            (telegram_id, display_name, role, added_by),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_user(self, telegram_id):
        """Get user by telegram ID"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT * FROM users WHERE telegram_id = %s
        """,
            (telegram_id,),
        )

        user = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(user) if user else None

    def remove_user(self, telegram_id):
        """Remove a user"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM users WHERE telegram_id = %s
        """,
            (telegram_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def update_user_role(self, telegram_id, new_role):
        """Update user's role"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE users SET role = %s WHERE telegram_id = %s
        """,
            (new_role, telegram_id),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_all_users(self):
        """Get all users"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT * FROM users ORDER BY role, display_name
        """
        )

        users = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(user) for user in users]

    def delete_non_superadmin_users(self, protected_ids):
        """Delete all users except those with protected IDs (original super admins)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Convert to tuple for SQL IN clause
        if protected_ids:
            placeholders = ','.join(['%s'] * len(protected_ids))
            cursor.execute(
                f"""
                DELETE FROM users WHERE telegram_id NOT IN ({placeholders})
            """,
                tuple(protected_ids),
            )
        else:
            cursor.execute("DELETE FROM users")

        deleted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        return deleted_count

    # ===== ENTRY MANAGEMENT =====

    def add_entry(self, uploaded_by, tag, content_data):
        """Add a new daily entry"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            INSERT INTO daily_entries (date, tag, content, uploaded_by)
            VALUES (%s, %s, %s, %s)
        """,
            (today, tag, json.dumps(content_data), uploaded_by),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_today_entries(self):
        """Get all entries for today"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT id, tag, content, uploaded_by, 
                   TO_CHAR(timestamp, 'HH24:MI') as timestamp
            FROM daily_entries 
            WHERE date = %s
            ORDER BY timestamp DESC
        """,
            (today,),
        )

        entries = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(entry) for entry in entries]

    def get_user_uploads_today(self, telegram_id):
        """Get user's uploads for today"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT id, tag, content, 
                   TO_CHAR(timestamp, 'HH24:MI') as timestamp
            FROM daily_entries 
            WHERE date = %s AND uploaded_by = %s
            ORDER BY timestamp DESC
        """,
            (today, telegram_id),
        )

        entries = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(entry) for entry in entries]

    def delete_entry_by_id(self, entry_id, telegram_id):
        """Delete a specific entry by ID (only if owned by user)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            DELETE FROM daily_entries 
            WHERE id = %s AND uploaded_by = %s AND date = %s
        """,
            (entry_id, telegram_id, today),
        )

        deleted = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        conn.close()

        return deleted

    def delete_all_user_uploads_today(self, telegram_id):
        """Delete all of user's uploads for today"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            DELETE FROM daily_entries 
            WHERE uploaded_by = %s AND date = %s
        """,
            (telegram_id, today),
        )

        deleted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        return deleted_count

    def purge_old_data(self):
        """Delete entries older than today and generate new code"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        # Delete old entries
        cursor.execute(
            """
            DELETE FROM daily_entries WHERE date < %s
        """,
            (today,),
        )

        deleted_count = cursor.rowcount

        # Delete old codes
        cursor.execute(
            """
            DELETE FROM daily_codes WHERE date < %s
        """,
            (today,),
        )

        # Delete old no-show reports first (due to foreign key)
        cursor.execute(
            """
            DELETE FROM noshow_reports WHERE date < %s
        """,
            (today,),
        )

        # Delete old relief reminders
        cursor.execute(
            """
            DELETE FROM relief_reminders WHERE date < %s
        """,
            (today,),
        )

        conn.commit()
        cursor.close()
        conn.close()

        # Clean up old files
        self._cleanup_old_files()

        # Generate new code
        self.generate_new_daily_code()

        return deleted_count

    # ===== FILE STORAGE =====

    def save_file(self, file_id, file_type, file_bytes):
        """Save file to storage and return path"""
        today = date.today().isoformat()
        today_dir = self.storage_path / today
        today_dir.mkdir(exist_ok=True)

        # Generate filename
        extension = "jpg" if file_type == "photo" else "pdf"
        filename = f"{file_id}.{extension}"
        file_path = today_dir / filename

        # Write file
        with open(file_path, "wb") as f:
            f.write(file_bytes)

        # Return relative path
        return str(file_path.relative_to(self.storage_path))

    def _cleanup_old_files(self):
        """Remove files from previous days"""
        today = date.today().isoformat()

        for day_dir in self.storage_path.iterdir():
            if day_dir.is_dir() and day_dir.name != today:
                # Delete all files in old directory
                for file in day_dir.iterdir():
                    file.unlink()
                # Remove directory
                day_dir.rmdir()

    # ===== DAILY CODE MANAGEMENT =====

    def generate_new_daily_code(self):
        """Generate new daily code"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        # Generate code: ANIMAL-DIGITS
        animal = random.choice(ANIMALS)
        digits = "".join(random.choices(string.digits, k=DAILY_CODE_LENGTH))
        code = f"{animal}-{digits}"

        cursor.execute(
            """
            INSERT INTO daily_codes (date, code)
            VALUES (%s, %s)
            ON CONFLICT (date) DO UPDATE SET code = %s, generated_at = CURRENT_TIMESTAMP
        """,
            (today, code, code),
        )

        conn.commit()
        cursor.close()
        conn.close()

        return code

    def get_daily_code(self):
        """Get today's code (generate if doesn't exist)"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT code FROM daily_codes WHERE date = %s
        """,
            (today,),
        )

        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            return result["code"]
        else:
            # Generate new code
            return self.generate_new_daily_code()

    # ===== RELIEF REMINDERS =====

    def add_relief_reminder(self, teacher_name, teacher_telegram_id, relief_time, period, 
                           class_info, room, original_teacher, created_by, activated=False):
        """Add a new relief reminder"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()
        
        # Convert time object to string if needed
        if hasattr(relief_time, 'strftime'):
            relief_time_str = relief_time.strftime('%H:%M:%S')
        else:
            relief_time_str = str(relief_time)

        cursor.execute(
            """
            INSERT INTO relief_reminders 
            (date, teacher_name, teacher_telegram_id, relief_time, period, class_info, room, original_teacher, created_by, activated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """,
            (today, teacher_name, teacher_telegram_id, relief_time_str, period, class_info, room, original_teacher, created_by, activated),
        )

        reminder_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        return reminder_id

    def get_today_relief_reminders(self):
        """Get all relief reminders for today"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT id, teacher_name, teacher_telegram_id, 
                   TO_CHAR(relief_time, 'HH24:MI') as relief_time,
                   period, class_info, room, original_teacher, 
                   reminder_sent, activated, created_by,
                   TO_CHAR(created_at, 'HH24:MI') as created_at
            FROM relief_reminders 
            WHERE date = %s
            ORDER BY relief_time ASC
        """,
            (today,),
        )

        reminders = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(r) for r in reminders]

    def get_pending_relief_reminders(self, current_time):
        """Get activated reminders that haven't been sent yet and are due"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT id, teacher_name, teacher_telegram_id, 
                   TO_CHAR(relief_time, 'HH24:MI') as relief_time,
                   period, class_info, room, original_teacher
            FROM relief_reminders 
            WHERE date = %s 
              AND activated = TRUE 
              AND reminder_sent = FALSE
              AND relief_time <= %s
            ORDER BY relief_time ASC
        """,
            (today, current_time),
        )

        reminders = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(r) for r in reminders]

    def mark_reminder_sent(self, reminder_id):
        """Mark a reminder as sent"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE relief_reminders SET reminder_sent = TRUE WHERE id = %s
        """,
            (reminder_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def activate_reminder(self, reminder_id, activate=True):
        """Activate or deactivate a reminder"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE relief_reminders SET activated = %s WHERE id = %s
        """,
            (activate, reminder_id),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def activate_all_matched_reminders(self):
        """Activate all reminders that have a matched telegram ID"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            UPDATE relief_reminders 
            SET activated = TRUE 
            WHERE date = %s AND teacher_telegram_id IS NOT NULL
        """,
            (today,),
        )

        updated = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        return updated

    def deactivate_all_reminders_today(self):
        """Deactivate all reminders for today"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            UPDATE relief_reminders SET activated = FALSE WHERE date = %s
        """,
            (today,),
        )

        updated = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        return updated

    def get_relief_reminder_by_id(self, reminder_id):
        """Get a specific relief reminder by ID"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, teacher_name, teacher_telegram_id, 
                   TO_CHAR(relief_time, 'HH24:MI') as relief_time,
                   period, class_info, room, original_teacher, 
                   reminder_sent, activated
            FROM relief_reminders 
            WHERE id = %s
        """,
            (reminder_id,),
        )

        reminder = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(reminder) if reminder else None

    def delete_relief_reminder(self, reminder_id):
        """Delete a relief reminder"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM relief_reminders WHERE id = %s
        """,
            (reminder_id,),
        )

        deleted = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        conn.close()

        return deleted

    def find_user_by_name(self, name):
        """Find a user by exact display name match"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT telegram_id, display_name, role FROM users 
            WHERE LOWER(display_name) = LOWER(%s)
        """,
            (name,),
        )

        user = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(user) if user else None

    # ===== NO-SHOW REPORTS =====

    def add_noshow_report(self, relief_reminder_id, teacher_name, reported_by, reporter_name, situation):
        """Add a no-show report"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            INSERT INTO noshow_reports 
            (date, relief_reminder_id, teacher_name, reported_by, reporter_name, situation)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """,
            (today, relief_reminder_id, teacher_name, reported_by, reporter_name, situation),
        )

        report_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        return report_id

    def get_today_noshow_reports(self):
        """Get all no-show reports for today"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        cursor.execute(
            """
            SELECT nr.id, nr.teacher_name, nr.reported_by, nr.reporter_name, 
                   nr.situation, TO_CHAR(nr.reported_at, 'HH24:MI') as reported_at,
                   rr.period, rr.class_info, rr.room,
                   TO_CHAR(rr.relief_time, 'HH24:MI') as relief_time
            FROM noshow_reports nr
            LEFT JOIN relief_reminders rr ON nr.relief_reminder_id = rr.id
            WHERE nr.date = %s
            ORDER BY nr.reported_at DESC
        """,
            (today,),
        )

        reports = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(r) for r in reports]

    # ===== GOOGLE DRIVE FOLDER MANAGEMENT =====

    def add_or_update_drive_folder(self, folder_name, drive_folder_id, parent_folder_id=None):
        """Add or update a drive folder"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO drive_folders (folder_name, drive_folder_id, parent_folder_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (drive_folder_id) 
            DO UPDATE SET folder_name = EXCLUDED.folder_name, parent_folder_id = EXCLUDED.parent_folder_id
            RETURNING id
        """,
            (folder_name, drive_folder_id, parent_folder_id),
        )

        folder_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        return folder_id

    def get_folder_by_name(self, folder_name):
        """Get folder by name"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, folder_name, drive_folder_id, parent_folder_id, last_synced_at
            FROM drive_folders 
            WHERE folder_name = %s
        """,
            (folder_name,),
        )

        folder = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(folder) if folder else None

    def get_folder_by_drive_id(self, drive_folder_id):
        """Get folder by Google Drive ID"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, folder_name, drive_folder_id, parent_folder_id, last_synced_at
            FROM drive_folders 
            WHERE drive_folder_id = %s
        """,
            (drive_folder_id,),
        )

        folder = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(folder) if folder else None

    def get_all_folders(self):
        """Get all folders"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, folder_name, drive_folder_id, parent_folder_id, last_synced_at
            FROM drive_folders 
            ORDER BY folder_name
        """
        )

        folders = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(f) for f in folders]

    def set_folder_role_access(self, folder_id, roles):
        """Set which roles can access a folder (replaces existing)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Delete existing role access
        cursor.execute(
            """
            DELETE FROM folder_role_access WHERE folder_id = %s
        """,
            (folder_id,),
        )

        # Add new role access
        for role in roles:
            cursor.execute(
                """
                INSERT INTO folder_role_access (folder_id, role)
                VALUES (%s, %s)
                ON CONFLICT (folder_id, role) DO NOTHING
            """,
                (folder_id, role.strip()),
            )

        conn.commit()
        cursor.close()
        conn.close()

    def get_folders_for_role(self, role):
        """Get all folders accessible to a role"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT DISTINCT df.id, df.folder_name, df.drive_folder_id, df.parent_folder_id, df.last_synced_at
            FROM drive_folders df
            INNER JOIN folder_role_access fra ON df.id = fra.folder_id
            WHERE fra.role = %s
            ORDER BY df.folder_name
        """,
            (role,),
        )

        folders = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(f) for f in folders]

    def get_folder_with_roles(self, folder_id):
        """Get folder with its role access list"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        # Get folder
        cursor.execute(
            """
            SELECT id, folder_name, drive_folder_id, parent_folder_id, last_synced_at
            FROM drive_folders 
            WHERE id = %s
        """,
            (folder_id,),
        )

        folder = cursor.fetchone()
        if not folder:
            cursor.close()
            conn.close()
            return None

        folder_dict = dict(folder)

        # Get roles
        cursor.execute(
            """
            SELECT role FROM folder_role_access WHERE folder_id = %s
        """,
            (folder_id,),
        )

        roles = [row["role"] for row in cursor.fetchall()]
        folder_dict["roles"] = roles

        cursor.close()
        conn.close()

        return folder_dict

    def update_folder_sync_time(self, folder_id):
        """Update last synced timestamp for a folder"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE drive_folders SET last_synced_at = CURRENT_TIMESTAMP WHERE id = %s
        """,
            (folder_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def log_sync(self, folder_id, files_synced, files_processed, errors, synced_by):
        """Log a sync operation"""
        conn = self.get_connection()
        cursor = conn.cursor()

        today = date.today()

        cursor.execute(
            """
            INSERT INTO drive_sync_log 
            (date, folder_id, files_synced, files_processed, errors, synced_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (today, folder_id, files_synced, files_processed, errors, synced_by),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_today_sync_logs(self, folder_id=None):
        """Get sync logs for today"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        today = date.today()

        if folder_id:
            cursor.execute(
                """
                SELECT sl.id, sl.files_synced, sl.files_processed, sl.errors, 
                       sl.synced_by, TO_CHAR(sl.synced_at, 'HH24:MI') as synced_at,
                       df.folder_name
                FROM drive_sync_log sl
                LEFT JOIN drive_folders df ON sl.folder_id = df.id
                WHERE sl.date = %s AND sl.folder_id = %s
                ORDER BY sl.synced_at DESC
            """,
                (today, folder_id),
            )
        else:
            cursor.execute(
                """
                SELECT sl.id, sl.files_synced, sl.files_processed, sl.errors, 
                       sl.synced_by, TO_CHAR(sl.synced_at, 'HH24:MI') as synced_at,
                       df.folder_name
                FROM drive_sync_log sl
                LEFT JOIN drive_folders df ON sl.folder_id = df.id
                WHERE sl.date = %s
                ORDER BY sl.synced_at DESC
            """,
                (today,),
            )

        logs = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(l) for l in logs]

    # ===== WEBHOOK MANAGEMENT =====

    def save_webhook(self, folder_id, channel_id, resource_id, webhook_url, page_token=None, expires_at=None):
        """Save webhook channel information"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO drive_webhooks 
            (folder_id, channel_id, resource_id, webhook_url, page_token, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (channel_id) 
            DO UPDATE SET resource_id = EXCLUDED.resource_id, 
                         page_token = EXCLUDED.page_token,
                         expires_at = EXCLUDED.expires_at,
                         active = TRUE
            RETURNING id
        """,
            (folder_id, channel_id, resource_id, webhook_url, page_token, expires_at),
        )

        webhook_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        return webhook_id

    def get_webhook_by_folder(self, folder_id):
        """Get active webhook for a folder"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, folder_id, channel_id, resource_id, webhook_url, page_token, expires_at
            FROM drive_webhooks 
            WHERE folder_id = %s AND active = TRUE
            ORDER BY created_at DESC
            LIMIT 1
        """,
            (folder_id,),
        )

        webhook = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(webhook) if webhook else None

    def update_webhook_page_token(self, channel_id, page_token):
        """Update the page token for a webhook"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE drive_webhooks SET page_token = %s WHERE channel_id = %s
        """,
            (page_token, channel_id),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def deactivate_webhook(self, channel_id):
        """Deactivate a webhook"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE drive_webhooks SET active = FALSE WHERE channel_id = %s
        """,
            (channel_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_all_active_webhooks(self):
        """Get all active webhooks"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT id, folder_id, channel_id, resource_id, webhook_url, page_token, expires_at
            FROM drive_webhooks 
            WHERE active = TRUE
        """
        )

        webhooks = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(w) for w in webhooks]

    # ===== SHORTCUT TARGET TRACKING =====

    def save_shortcut_target(self, shortcut_id, shortcut_name, target_file_id, target_file_name, watched_folder_id):
        """Save a shortcut and its target file for tracking"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO shortcut_targets 
            (shortcut_id, shortcut_name, target_file_id, target_file_name, watched_folder_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (shortcut_id, target_file_id) 
            DO UPDATE SET shortcut_name = EXCLUDED.shortcut_name,
                         target_file_name = EXCLUDED.target_file_name
            RETURNING id
        """,
            (shortcut_id, shortcut_name, target_file_id, target_file_name, watched_folder_id),
        )

        target_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()

        return target_id

    def get_shortcut_targets_for_folder(self, watched_folder_id):
        """Get all shortcut targets being watched for a folder"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT shortcut_id, shortcut_name, target_file_id, target_file_name
            FROM shortcut_targets 
            WHERE watched_folder_id = %s
        """,
            (watched_folder_id,),
        )

        targets = cursor.fetchall()
        cursor.close()
        conn.close()

        return [dict(t) for t in targets]

    def get_shortcut_by_target(self, target_file_id):
        """Get shortcut info by target file ID"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        cursor.execute(
            """
            SELECT shortcut_id, shortcut_name, target_file_id, target_file_name, watched_folder_id
            FROM shortcut_targets 
            WHERE target_file_id = %s
            LIMIT 1
        """,
            (target_file_id,),
        )

        shortcut = cursor.fetchone()
        cursor.close()
        conn.close()

        return dict(shortcut) if shortcut else None

    def remove_shortcut_target(self, shortcut_id):
        """Remove a shortcut target from tracking"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM shortcut_targets WHERE shortcut_id = %s
        """,
            (shortcut_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    # ===== STATISTICS =====

    def get_stats(self):
        """Get bot usage statistics"""
        conn = self.get_connection()
        cursor = conn.cursor(row_factory=dict_row)

        # Count users by role
        cursor.execute(
            """
            SELECT role, COUNT(*) as count
            FROM users
            GROUP BY role
        """
        )

        role_counts = {row["role"]: row["count"] for row in cursor.fetchall()}

        # Count today's entries
        today = date.today()
        cursor.execute(
            """
            SELECT COUNT(*) as count
            FROM daily_entries
            WHERE date = %s
        """,
            (today,),
        )

        today_count = cursor.fetchone()["count"]

        cursor.close()
        conn.close()

        return {
            "total_users": sum(role_counts.values()),
            "superadmins": role_counts.get("superadmin", 0),
            "admin": role_counts.get("admin", 0),
            "relief_member": role_counts.get("relief_member", 0),
            "viewers": role_counts.get("viewer", 0),
            "today_entries": today_count,
        }
