import os
import json
import random
import string
from datetime import datetime, date
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
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
        return psycopg2.connect(self.db_url)

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
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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

    # ===== STATISTICS =====

    def get_stats(self):
        """Get bot usage statistics"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

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
            "uploadadmins": role_counts.get("uploadadmin", 0),
            "uploaders": role_counts.get("uploader", 0),
            "viewers": role_counts.get("viewer", 0),
            "today_entries": today_count,
        }
