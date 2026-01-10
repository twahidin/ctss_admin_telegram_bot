# Quick Start Guide

## 🚀 Local Testing (5 minutes)

### 1. Get your credentials

**Telegram Bot Token:**
1. Open Telegram, find @BotFather
2. Send: `/newbot`
3. Follow prompts, save your token

**Your Telegram ID:**
1. Find @userinfobot on Telegram
2. Send: `/start`
3. Note your ID number

**Claude API Key:**
1. Go to: console.anthropic.com
2. Create API key
3. Save it

### 2. Quick setup

```bash
# Clone repo (if from git)
git clone <repo-url>
cd school_admin_bot

# Install
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Setup database (local PostgreSQL)
createdb schooladmin

# Or use Docker
docker run --name postgres -e POSTGRES_PASSWORD=password -p 5432:5432 -d postgres
docker exec -it postgres createdb -U postgres schooladmin

# Configure
cp .env.example .env
# Edit .env with your credentials

# Initialize
python setup.py

# Test
python test_config.py

# Run
python main.py
```

### 3. Test in Telegram

1. Find your bot on Telegram
2. Send: `/start`
3. Send: `/help`
4. Send: `/code` (get today's upload code)
5. Send: `/upload` (try uploading)

---

## ☁️ Railway Deployment (10 minutes)

### 1. Prepare code

```bash
git init
git add .
git commit -m "Initial commit"
git push origin main  # Push to GitHub
```

### 2. Setup Railway

1. Go to railway.app
2. Sign up with GitHub
3. New Project → Deploy from GitHub
4. Select your repo
5. Add PostgreSQL database (+ New → Database → PostgreSQL)

### 3. Set environment variables

In Railway project settings, add:

```
TELEGRAM_TOKEN=your_bot_token
CLAUDE_API_KEY=your_api_key
SUPER_ADMIN_IDS=your_telegram_id
```

(DATABASE_URL is auto-set by PostgreSQL addon)

### 4. Deploy

Railway auto-deploys on git push!

Check logs to verify it's running.

---

## 📝 First Steps After Deployment

### Add your first user

```
You (super admin): /start
Bot: Registers you as super admin

You: /add 123456789
Bot: Adds user as viewer

You: /promote 123456789 uploader
Bot: Promotes to uploader
```

### Upload information

```
You: /upload
Bot: Select category (1-6)
You: 1
Bot: Send photo/PDF or type
You: [sends image]
Bot: Enter code
You: TIGER-1234
Bot: ✅ Saved!
```

### Query information

```
You: /ask Who's teaching 3A at 10am?
Bot: Based on today's info... [answer]
```

---

## 🔧 Common Commands Reference

### Everyone
- `/start` - Register
- `/help` - Show commands
- `/ask [question]` - Query info
- `/today` - View summary

### Uploaders
- `/upload` - Upload info
- `/code` - Get upload code
- `/myuploads` - View my uploads

### Admins
- `/add [id]` - Add viewer
- `/remove [id]` - Remove user
- `/list` - List all users
- `/promote [id] [role]` - Promote user
- `/stats` - View statistics
- `/newcode` - New upload code
- `/purge` - Manual data purge

---

## ⚠️ Troubleshooting

**Bot not responding?**
- Check Railway logs
- Verify all env vars are set
- Test Telegram token with test_config.py

**Database errors?**
- Ensure PostgreSQL is running
- Verify DATABASE_URL
- Check Railway database addon

**Upload code not working?**
- Code changes at midnight
- Use `/newcode` to generate fresh
- Verify user has uploader role

**Files not saving?**
- Check storage path exists
- On Railway, add volume mount
- Verify disk space

---

## 💰 Cost Estimate

**Testing locally:** FREE (except Claude API ~$2/month)

**Production (Railway):**
- Railway: ~$10/month
- Claude API: ~$2-3/month
- **Total: ~$13/month**

---

## 🎯 Next Steps

1. **Test thoroughly locally** before deploying
2. **Add your first 5 users** to test workflows
3. **Monitor costs** in first week
4. **Collect feedback** from staff
5. **Iterate based on usage**

---

**Questions? Check the full README.md**
