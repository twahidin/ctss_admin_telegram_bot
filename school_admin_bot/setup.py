#!/usr/bin/env python3
"""
Setup script for School Admin Bot
Initializes database and creates first super admin
"""

import sys
from database import Database
from config import SUPER_ADMIN_IDS

def main():
    print("🚀 School Admin Bot - Setup")
    print("=" * 50)
    
    try:
        # Initialize database
        print("\n📦 Initializing database...")
        db = Database()
        print("✅ Database tables created successfully!")
        
        # Create super admins
        print(f"\n👑 Creating super admin(s)...")
        for admin_id in SUPER_ADMIN_IDS:
            db.add_user(admin_id, f"SuperAdmin_{admin_id}", "superadmin", admin_id)
            print(f"✅ Super admin {admin_id} added")
        
        # Generate first daily code
        print("\n🔐 Generating daily code...")
        code = db.get_daily_code()
        print(f"✅ Today's upload code: {code}")
        
        print("\n" + "=" * 50)
        print("✨ Setup complete!")
        print("\nNext steps:")
        print("1. Run: python main.py")
        print("2. Open Telegram and message your bot")
        print("3. Send /start to register")
        print(f"4. Use /code to get upload code: {code}")
        print("\n💡 Need help? Check README.md")
        
    except Exception as e:
        print(f"\n❌ Setup failed: {e}")
        print("\nTroubleshooting:")
        print("1. Check your .env file exists and has correct values")
        print("2. Ensure PostgreSQL is running")
        print("3. Verify DATABASE_URL is correct")
        sys.exit(1)

if __name__ == "__main__":
    main()
