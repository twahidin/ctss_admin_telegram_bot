# School Admin Info Bot

A Telegram bot for managing daily school administrative information with role-based access control, file uploads, and AI-powered queries using Claude.

## Features

- ✅ **Role-based access control** (Viewers, Uploaders, Upload Admins, Super Admins)
- 📤 **Multi-format uploads** (text, images, PDFs) with predefined categories
- 🔐 **Daily rotating access codes** for uploads
- 🤖 **AI-powered queries** using Claude Haiku
- 🗑️ **Automatic daily data purge** at midnight
- 📊 **Usage statistics** and admin controls
- ☁️ **Cloud-ready** for Railway deployment

## Architecture

```
Input: Telegram Bot (private DMs only)
Auth: Three-tier access (Viewer → Uploader → Upload Admin)
Storage: PostgreSQL + Railway volumes
AI: Claude Haiku API for intelligent queries
Cleanup: Daily cron job (midnight SGT)
```

## Prerequisites

1. **Telegram Bot Token**
   - Talk to [@BotFather](https://t.me/BotFather) on Telegram
   - Create a new bot: `/newbot`
   - Save your token

2. **Claude API Key**
   - Sign up at [console.anthropic.com](https://console.anthropic.com)
   - Generate an API key
   - Note: ~$2-3/month for 100 queries/day

3. **PostgreSQL Database**
   - Local: Install PostgreSQL locally
   - Railway: Automatically provisioned

4. **Your Telegram ID**
   - Message [@userinfobot](https://t.me/userinfobot) on Telegram
   - Note your ID number

## Local Setup

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd school_admin_bot
```

### 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Setup PostgreSQL locally

```bash
# Install PostgreSQL (macOS)
brew install postgresql
brew services start postgresql

# Create database
createdb schooladmin

# Or use Docker
docker run --name postgres -e POSTGRES_PASSWORD=password -p 5432:5432 -d postgres
docker exec -it postgres createdb -U postgres schooladmin
```

### 5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` file:

```env
TELEGRAM_TOKEN=your_bot_token_from_botfather
CLAUDE_API_KEY=your_claude_api_key
DATABASE_URL=postgresql://postgres:password@localhost:5432/schooladmin
SUPER_ADMIN_IDS=your_telegram_id
STORAGE_PATH=./data/uploads
```

### 6. Run the bot

```bash
python main.py
```

### 7. Test locally

1. Open Telegram and find your bot
2. Send `/start` - you should be registered as super admin
3. Send `/help` - see all commands
4. Send `/code` - get today's upload code
5. Try `/upload` and follow the flow
6. Test queries with `/ask who is teaching 3A?`

## Railway Deployment

### 1. Prepare your repository

```bash
git init
git add .
git commit -m "Initial commit"
```

### 2. Create Railway account

- Go to [railway.app](https://railway.app)
- Sign up with GitHub

### 3. Create new project

1. Click **"New Project"**
2. Select **"Deploy from GitHub repo"**
3. Choose your repository

### 4. Add PostgreSQL database

1. In your project, click **"+ New"**
2. Select **"Database"** → **"PostgreSQL"**
3. Railway will automatically create `DATABASE_URL` variable

### 5. Configure environment variables

In your Railway project settings, add:

```
TELEGRAM_TOKEN=your_bot_token
CLAUDE_API_KEY=your_claude_api_key
SUPER_ADMIN_IDS=your_telegram_id
STORAGE_PATH=/app/data/uploads
```

Note: `DATABASE_URL` is automatically set by Railway's PostgreSQL addon.

### 6. Deploy

Railway will automatically deploy on every git push:

```bash
git push origin main
```

### 7. Add volume for file storage (optional)

1. In Railway project, click your service
2. Go to **"Settings"** → **"Volumes"**
3. Add volume: `/app/data`

### 8. Monitor logs

- Click on your service
- Go to **"Deployments"** tab
- Click on latest deployment to see logs

## Usage Guide

### User Roles

1. **Viewers** (default)
   - Query information: `/ask [question]`
   - View today's summary: `/today`

2. **Uploaders**
   - Upload information: `/upload`
   - View their uploads: `/myuploads`
   - Get upload code: `/code`

3. **Upload Admins**
   - All uploader permissions
   - Add viewers: `/add [telegram_id]`
   - Remove users: `/remove [telegram_id]`
   - List users: `/list`

4. **Super Admins**
   - All permissions
   - Promote users: `/promote [telegram_id] [role]`
   - Generate new codes: `/newcode`
   - View statistics: `/stats`
   - Manual purge: `/purge`

### Information Categories

1. **RELIEF** - Teacher covering classes
2. **ABSENT** - Staff away/on leave
3. **EVENT** - Special activities
4. **VENUE_CHANGE** - Room/location changes
5. **DUTY_ROSTER** - Who's on duty
6. **GENERAL** - Other announcements

### Upload Flow

```
1. User: /upload
2. Bot: Select category (1-6)
3. User: 1 (for RELIEF)
4. Bot: Send photo/PDF or type message
5. User: [uploads timetable image]
6. Bot: Enter today's code
7. User: TIGER-1234
8. Bot: ✅ Saved!
```

### Query Examples

```
/ask Who's teaching 3A at 10am?
/ask Who's absent today?
/ask What events are happening?
/ask Where is the morning assembly?
```

## Daily Automation

The bot automatically:
- **Purges old data** at midnight (SGT)
- **Generates new upload code** daily
- **Notifies super admins** of reset

## Cost Breakdown

### Monthly Costs

- **Railway**: $10.50/month
  - PostgreSQL: $5
  - Web service: $5
  - Storage: $0.50

- **Claude API**: $2-3/month
  - 100 queries/day × 30 days
  - Haiku pricing: ~$0.10/day

**Total: ~$13/month**

### Free Tier Alternative

For testing, use:
- Railway free tier (500 hours/month)
- Local PostgreSQL
- Cost: Just Claude API (~$2-3/month)

## Troubleshooting

### Bot not responding

```bash
# Check Railway logs
# Look for error messages
# Verify environment variables are set
```

### Database connection failed

```bash
# Verify DATABASE_URL is correct
# Check PostgreSQL service is running on Railway
# Test connection locally first
```

### Upload code not working

```bash
# Check if code expired (changes at midnight)
# Use /newcode to generate fresh code (super admin only)
# Verify user has uploader role
```

### File uploads failing

```bash
# Check storage path exists
# On Railway, verify volume is mounted
# Check disk space
```

## Security Best Practices

1. **Never commit `.env` file** to git
2. **Rotate API keys** periodically
3. **Limit super admin access** to 2-3 people
4. **Monitor upload activity** via `/stats`
5. **Review user list** regularly with `/list`

## Maintenance

### Adding new categories

Edit `config.py`:

```python
TAGS = [
    "RELIEF",
    "ABSENT",
    # Add your new category
    "NEW_CATEGORY",
]
```

Redeploy to Railway.

### Changing purge time

Edit `main.py`:

```python
# Change midnight to another time
job_queue.run_daily(
    self.daily_purge_job,
    time=time(hour=0, minute=0),  # Modify hours
)
```

### Database backup

```bash
# On Railway, use Railway CLI
railway run pg_dump > backup.sql

# Restore
railway run psql < backup.sql
```

## Support

For issues or questions:
1. Check logs on Railway
2. Review error messages
3. Test commands locally first
4. Use `/stats` to see bot health

## License

MIT License - feel free to modify for your school's needs.

---

**Built for schools in Singapore 🇸🇬**
