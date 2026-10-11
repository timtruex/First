# Old Walls, New Memories

*Sleeping in history, one old building at a time.*

An agent-run Instagram / TikTok / newsletter brand featuring historic places you
can actually stay in — schoolhouses, churches, depots, mills, lighthouses,
firehouses — anchored by our own 1875 schoolhouse and creekside cottage in
Bellville, Ohio.

## How it runs

```
            Tim  (≈3 hrs/week: approve, send emails, film, reply)
             ▲ │
   weekly    │ │ decisions go in ops/inbox.md (or reply in the GM session)
   brief     │ ▼
     ┌──────────────────────┐
     │   General Manager    │  weekly scheduled run — agents/general-manager.md
     └──────────┬───────────┘
   ┌──────────┬─┴────────┬───────────┬───────────┐
 Scout    Historian   Producer    Outreach    Analyst
 finds    researches  writes      drafts host  weekly
 stays    true story  posts       emails       numbers
```

Specialists live in `.claude/agents/`. Tim only deals with the General Manager.
All state lives in this repo, so every weekly run starts fresh and cheap
instead of dragging months of conversation history along.

## Layout

| Path | What it holds |
|---|---|
| `brand/brand.md` | Name, voice, bio, colors, content pillars, hard rules |
| `data/stays.csv` | Pipeline of historic stays (prospect → contacted → approved → posted) |
| `content/calendar.md` | Rolling 4-week posting calendar |
| `content/drafts/` | One file per post: hook, script, shot list, caption, status |
| `outreach/templates.md` | Host permission / feature emails |
| `owner/` | Our two properties — facts, photo list, listing rewrite |
| `ops/inbox.md` | Tim → GM: approvals, decisions, notes |
| `ops/decisions.md` | Standing rules Tim has set (GM reads every run) |
| `ops/state.md` | GM's working memory: artifact URL, counters, open loops |
| `reports/` | Weekly briefs |

## Hard rules (summary — full list in `brand/brand.md`)

1. Never post a photo or video we don't have written permission to use.
2. No AI-generated images presented as a real place.
3. Every historical claim gets a source in the draft before it's approved.
4. No grandchildren's faces, names, teams, or schools without their parents' OK.
5. Paid features are disclosed (`#ad` / "paid partnership"), per FTC rules.
6. Nothing posts without Tim's approval.
