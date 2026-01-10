#!/usr/bin/env python3
"""
Test configuration and connectivity
"""

import sys

def test_imports():
    """Test if all required packages are installed"""
    print("📦 Testing imports...")
    try:
        import telegram
        print("✅ python-telegram-bot")
    except ImportError:
        print("❌ python-telegram-bot - run: pip install -r requirements.txt")
        return False
        
    try:
        import anthropic
        print("✅ anthropic")
    except ImportError:
        print("❌ anthropic - run: pip install -r requirements.txt")
        return False
        
    try:
        import psycopg2
        print("✅ psycopg2")
    except ImportError:
        print("❌ psycopg2 - run: pip install -r requirements.txt")
        return False
        
    return True

def test_config():
    """Test if configuration is valid"""
    print("\n⚙️  Testing configuration...")
    try:
        from config import TELEGRAM_TOKEN, CLAUDE_API_KEY, DATABASE_URL, SUPER_ADMIN_IDS
        
        if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
            print("❌ TELEGRAM_TOKEN not set in .env")
            return False
        print(f"✅ TELEGRAM_TOKEN (ends with: ...{TELEGRAM_TOKEN[-8:]})")
        
        if not CLAUDE_API_KEY or CLAUDE_API_KEY == "your_claude_api_key_here":
            print("❌ CLAUDE_API_KEY not set in .env")
            return False
        print(f"✅ CLAUDE_API_KEY (starts with: {CLAUDE_API_KEY[:8]}...)")
        
        if not DATABASE_URL or "localhost" in DATABASE_URL:
            print(f"⚠️  DATABASE_URL set to local: {DATABASE_URL[:50]}...")
        else:
            print(f"✅ DATABASE_URL configured")
            
        if not SUPER_ADMIN_IDS:
            print("❌ SUPER_ADMIN_IDS not set in .env")
            return False
        print(f"✅ SUPER_ADMIN_IDS: {SUPER_ADMIN_IDS}")
        
        return True
        
    except Exception as e:
        print(f"❌ Config error: {e}")
        return False

def test_database():
    """Test database connection"""
    print("\n🗄️  Testing database connection...")
    try:
        from database import Database
        db = Database()
        print("✅ Database connection successful")
        
        # Test basic operations
        stats = db.get_stats()
        print(f"✅ Database operational - {stats['total_users']} users")
        
        return True
        
    except Exception as e:
        print(f"❌ Database error: {e}")
        print("\nTips:")
        print("- Is PostgreSQL running?")
        print("- Is DATABASE_URL correct?")
        print("- Try: createdb schooladmin")
        return False

def test_telegram():
    """Test Telegram bot token"""
    print("\n📱 Testing Telegram bot...")
    try:
        from telegram import Bot
        from config import TELEGRAM_TOKEN
        
        bot = Bot(token=TELEGRAM_TOKEN)
        bot_info = bot.get_me()
        print(f"✅ Connected to bot: @{bot_info.username}")
        print(f"   Name: {bot_info.first_name}")
        
        return True
        
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        print("\nTips:")
        print("- Is your bot token correct?")
        print("- Get token from @BotFather")
        return False

def main():
    print("🧪 School Admin Bot - Configuration Test")
    print("=" * 50)
    
    results = []
    
    # Run all tests
    results.append(("Imports", test_imports()))
    results.append(("Configuration", test_config()))
    results.append(("Database", test_database()))
    results.append(("Telegram", test_telegram()))
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Test Results:")
    
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
        if not passed:
            all_passed = False
    
    print("=" * 50)
    
    if all_passed:
        print("\n🎉 All tests passed! You're ready to run the bot.")
        print("\nNext step: python main.py")
    else:
        print("\n⚠️  Some tests failed. Please fix the issues above.")
        print("\nNeed help? Check README.md")
        sys.exit(1)

if __name__ == "__main__":
    main()
