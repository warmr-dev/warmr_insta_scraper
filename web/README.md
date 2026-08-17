# Warmr Admin

Admin dashboard for the Instagram stories monitor. Reads the same Supabase
database the scraper writes to.

Sibling project to `ai_stories_scraper` — it only reads analytics and manages
sessions; it never talks to Instagram except to verify that a session works.

---

## Run it

```bash
npm install
cp .env.example .env.local     # fill in DATABASE_URL, ADMIN_EMAIL, ADMIN_PASSWORD, SESSION_SECRET
npm run dev
```

Then open the printed URL and sign in.

Generate a session secret with:

```bash
openssl rand -hex 32
```

---

## Pages

| Page | What it shows |
|---|---|
| **Overview** | Funnel and cost at a glance, last 14 days, pipeline states, score distribution |
| **Accounts & Sessions** | Live/dead sessions, upload cookies, re-check, remove |
| **Leads** | Every story scoring 7+, with category, intent and the AI's reasoning |
| **Monitored Accounts** | Who posts regularly, and who the pipeline stopped paying to analyse |
| **Analytics** | Funnel percentages, cost per photo and per lead, categories detected |

---

## Sessions

Instagram web sessions expire after a few weeks and **cannot be renewed from
code** — there is no login flow on the web API. When one dies, collection stops
until fresh cookies are pasted in.

The dashboard makes that routine rather than an emergency:

1. **+ Add session** — upload the JSON a cookie-export extension produces, or
   paste the `a=1; b=2` string from DevTools. Both shapes are accepted, so
   nothing has to be reformatted by hand.
2. Before saving, the cookies are **verified against Instagram**. Saving a dead
   session would only mean the loop silently skips it later.
3. **Re-check** tests a stored session on demand; **Open Instagram to renew**
   opens the site so you can grab fresh cookies.

Seven cookies are required: `sessionid`, `csrftoken`, `ds_user_id`, `ig_did`,
`mid`, `datr`, `rur`. With fewer, Instagram's feed endpoints answer 302 — the
table shows a count so a partial paste is obvious.

The health check uses `feed/reels_tray/`, the endpoint the scraper actually
depends on. `accounts/current_user/` answers 400 to browser cookies even when
the feeds work, so it is useless as an indicator.

---

## Security

**The database is deny-by-default.** Migration `0003` in the scraper enables RLS
on all 11 tables with zero policies and revokes privileges from `anon` and
`authenticated`, so a leaked Supabase anon key reads nothing.

That is why **every query here runs server-side**. `lib/db.ts` throws if it is
ever imported into a browser bundle. The reason is specific: the `cookies` table
holds live Instagram sessions, so exposing it would hand over working sessions,
not merely analytics.

Do not add anon read policies to make browser-side queries work.

The session cookie is `httpOnly` and HMAC-signed with `SESSION_SECRET`, so it
cannot be forged by editing its value. Credential comparison is constant-time,
and a wrong email costs the same as a wrong password.

---

## Deploy

```bash
npx vercel --prod
```

Set `DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` and `SESSION_SECRET` as
environment variables in the host. Every page is `force-dynamic` — the numbers
are live, so nothing is cached at build time.
