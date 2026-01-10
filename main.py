# Entry point for Railway deployment
# This imports and runs the bot from school_admin_bot directory

import sys
import os

# Add school_admin_bot to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'school_admin_bot'))

# Now import and run the actual bot
from main import SchoolAdminBot

if __name__ == "__main__":
    bot = SchoolAdminBot()
    bot.run()
