# Railway Deployment Checklist ✅

## Pre-Deployment

- [ ] Tested bot locally with real Telegram account
- [ ] Verified all commands work (/start, /upload, /ask, etc.)
- [ ] Tested file uploads (photos, PDFs, text)
- [ ] Confirmed Claude API queries work
- [ ] Database operations tested (add user, upload, query, purge)
- [ ] Got your Telegram ID from @userinfobot
- [ ] Got bot token from @BotFather
- [ ] Got Claude API key from console.anthropic.com

## Git Setup

- [ ] Created GitHub repository
- [ ] Added all files to git
- [ ] Created `.gitignore` (already included)
- [ ] Committed code: `git commit -m "Initial commit"`
- [ ] Pushed to GitHub: `git push origin main`

## Railway Setup

- [ ] Created Railway account (railway.app)
- [ ] Connected GitHub account
- [ ] Created new project
- [ ] Selected your repository
- [ ] Deployment triggered automatically

## Database Setup

- [ ] Added PostgreSQL database (+ New → Database → PostgreSQL)
- [ ] Waited for database to provision
- [ ] Verified DATABASE_URL environment variable exists

## Environment Variables

Go to Railway project → Settings → Variables

- [ ] `TELEGRAM_TOKEN` = your_bot_token
- [ ] `CLAUDE_API_KEY` = your_api_key  
- [ ] `SUPER_ADMIN_IDS` = your_telegram_id
- [ ] `STORAGE_PATH` = /app/data/uploads
- [ ] `DATABASE_URL` = (auto-set by PostgreSQL addon)

## Optional: Add Volume

For persistent file storage:

- [ ] Go to Settings → Volumes
- [ ] Add volume: `/app/data`
- [ ] Save and redeploy

## Verify Deployment

- [ ] Check deployment logs (no errors)
- [ ] See "Bot started successfully!" in logs
- [ ] No database connection errors
- [ ] No missing environment variable errors

## Test in Production

- [ ] Find bot on Telegram
- [ ] Send `/start` - should register you as super admin
- [ ] Send `/help` - should show all commands
- [ ] Send `/code` - should show today's upload code
- [ ] Send `/upload` - should work through flow
- [ ] Send `/ask test` - should query with Claude
- [ ] Send `/stats` - should show statistics

## Add First Users

- [ ] Ask colleague for their Telegram ID
- [ ] Use `/add [telegram_id]` to add as viewer
- [ ] Have them send `/start` to register
- [ ] Promote if needed: `/promote [id] uploader`

## Monitor First Week

- [ ] Check Railway usage dashboard daily
- [ ] Monitor Claude API costs in console.anthropic.com
- [ ] Review logs for any errors
- [ ] Collect feedback from users
- [ ] Watch `/stats` for usage patterns

## Ongoing Maintenance

- [ ] Set up budget alerts on Railway
- [ ] Set up budget alerts on Claude console
- [ ] Weekly check of `/stats` 
- [ ] Monthly user list review (`/list`)
- [ ] Backup database monthly (Railway CLI: `railway run pg_dump`)

## Troubleshooting Commands

If something goes wrong:

```bash
# View live logs
railway logs

# Connect to database
railway connect

# Restart service
railway restart

# Check environment variables
railway variables
```

## Emergency Rollback

If deployment fails:

1. Go to Railway → Deployments
2. Find last working deployment
3. Click "Redeploy"
4. Or: `git revert HEAD` and push

## Cost Monitoring

- [ ] Railway: Check usage in dashboard
- [ ] Claude: Check API usage in console
- [ ] Set alerts at $20/month threshold
- [ ] Expected: ~$13/month total

## Security Checklist

- [ ] `.env` file NOT committed to git
- [ ] API keys NOT in code
- [ ] Only trusted users as super admins (2-3 max)
- [ ] Regular user list review
- [ ] Monitor upload patterns for abuse

---

## 🎉 Deployment Complete!

When everything is checked:

1. Inform your team
2. Share bot username
3. Provide initial upload code
4. Collect feedback
5. Iterate!

**Support:** Check logs first, then README.md for troubleshooting.

---

**Deployed on:** _______________

**Bot username:** @_______________

**Initial upload code:** _______________

**Super admins:** _______________
