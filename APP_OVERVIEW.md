# LinkedIn Comment Generator — Application Overview

> A personal engagement-intelligence tool that turns the daily "scroll LinkedIn and try to comment on the right posts" chore into a 5-minute curated morning workflow. AI drafts six distinct comment options per post; the human picks, edits, and posts.

---

## The problem

Thoughtful LinkedIn engagement compounds — the people you helpfully respond to convert into opportunities, hires, and warm intros months later. But the daily mechanic is punishing:

- The feed is noisy; the people who matter are buried.
- Writing a fresh, on-tone comment five to ten times a day is decision fatigue.
- The engagement *strategy* lives only in your head — never structured, never searchable.
- Without a system, you over-engage some weeks and ghost others.

---

## The solution at a glance

```mermaid
flowchart LR
    A[Curated handles] --> B[Daily fetch · 6am]
    B --> C[Apify: latest post per handle]
    C --> D[Claude: 6 tones per post]
    D --> E[Morning dashboard]
    E --> F[Human: review, edit, copy]
    F --> G[Human: paste into LinkedIn]
    G --> H[Posted log + ★ ratings]
    H -.feedback.-> E
```

Posting is always a human action — the app **never writes to LinkedIn**. This is intentional: it's a productivity tool, not a bot.

---

## End-to-end workflow

```mermaid
flowchart TB
    subgraph Curation["1 · Curation (one-time, then ongoing)"]
        H1[Add LinkedIn handles] --> H2["Auto-tag untagged<br/>(one click)"]
        H2 --> H3[(Tagged handle list:<br/>persona · reach · intent · cadence)]
    end

    subgraph Agent["2 · Daily Agent (unattended, 6am)"]
        DA1[For each active handle] --> DA2[Apify: fetch latest post]
        DA2 --> DA3[Dedupe by post ID]
        DA3 --> DA4[Claude: draft 6 tone variations]
    end

    subgraph Review["3 · Morning Review (~5 min, human)"]
        R1[Open dashboard] --> R2[Skim post + 6 options]
        R2 --> R3[Edit or regenerate]
        R3 --> R4[Copy chosen comment]
        R4 --> R5[Paste into LinkedIn]
        R5 --> R6[Mark posted + rate ★1-5]
    end

    subgraph Intel["4 · Activation Intelligence (when you have time)"]
        I1[Recommendation panel] --> I2[Score inactive handles]
        I2 --> I3[One-click activate<br/>top candidates]
    end

    H3 --> DA1
    DA4 --> R1
    R6 -.history.-> Intel
```

---

## Feature map — three user jobs

### 1 · Build & maintain a watch list
*Routes: `/admin`, `/admin/tags`*

Add LinkedIn handles, tag them across four dimensions, filter the list by tag, toggle each one active/inactive. The tag taxonomy itself is editable.

| Dimension | Why it matters | Examples |
|---|---|---|
| **Persona** | Drives tone selection | founder, ceo, recruiter, investor-vc, investor-pe |
| **Reach** | Drives comment-visibility ROI | mega (100k+), large (10–100k), mid, niche |
| **Intent** | Drives skip-or-prioritise decisions | prospect, network, hiring-signal, thought-leader |
| **Cadence** | Drives whether daily fetch is worth it | daily, weekly, sporadic |

### 2 · Generate daily comments
*Runs unattended at 6 am via APScheduler*

For each active handle: fetch latest post via Apify → dedupe → ask Claude to draft six comments, one per registered tone. All comments stored against the post.

The six tones (each independently editable in `/admin/tones`):

| Tone | Intent |
|---|---|
| **Operator** | Ground-level, practical execution perspective |
| **Strategic** | Big-picture, market or business angle |
| **Curious** | Question-led, invites dialogue |
| **Contrarian** | Respectful pushback or alternative view |
| **Affirming** | Builds on their point, adds a layer |
| **Concise** | One punchy sentence |

### 3 · Review & post
*Routes: `/dashboard`, `/posted`*

Two-pane dashboard: post on the left, six comment cards on the right. Each card supports copy / edit / mark-posted / regenerate. Posted comments get a 1–5★ rating; the `/posted` view aggregates by handle and tone so trends emerge.

---

## The intelligence layers (the executive-grade differentiators)

### A · Auto-tagging
One click ("Auto-tag untagged") calls Apify's profile scraper per handle, then:

- **Reach** — deterministic bucket from `followerCount`
- **Cadence** — median gap between recent posts → daily / weekly / sporadic
- **Persona** — Claude reads headline + current role + recent companies and picks 1–2 persona tags from a curated list
- **Display name** — reformatted to *"Name — Position, Company"* (with dedup when LinkedIn already embeds the company in the position)
- **Intent stays manual** — that's a strategic decision the human owns

### B · Activation intelligence

A scored recommendation panel surfaces dormant handles worth waking up today:

```
score = engagement + your-care + tag-strength − recency-cooldown
```

Every score is explained with on-screen "+/−" reason chips:

| Signal category | Examples |
|---|---|
| **Engagement** | `+3 posts:4`, `+2 rating 4.5★` |
| **Your-care** | `+2 notes`, `+1 display_name` |
| **Tag-strength** | `+3 prospect`, `+2 reach-large+`, `+1 persona`, `+1 cadence` |
| **Recency cooldown** | `−5 posted yesterday`, `−3 posted 2d ago`, `−1 posted 5d ago` |

Recency penalty is *waived* (`exempt:<reason>`) for high-signal intent:

```mermaid
flowchart LR
    A{"Has hiring-signal intent<br/>OR investor-vc/pe persona?"} -->|Yes| B[No recency penalty<br/>· always recommend]
    A -->|No| C{"Last posted within…"}
    C -->|≤1 day| D[−5]
    C -->|≤3 days| E[−3]
    C -->|≤7 days| F[−1]
    C -->|Older / never| G[no penalty]
```

Logic in plain English: *a recruiter who posted yesterday is still worth engaging today; a thought-leader who posted yesterday probably isn't.*

### C · Tone diversity
Six tones instead of one bland AI voice prevents tone-collapse. The mix means you always have a comment that fits the relationship.

### D · Compliance posture
The app reads via Apify (no login, no cookies) and writes nothing back to LinkedIn. All comment posting is a copy-paste action you take by hand.

---

## Architecture (one-glance)

```mermaid
flowchart LR
    User["User · browser"] -->|HTMX| FA[FastAPI app]
    FA -->|sqlite3| DB[(SQLite<br/>li_comments.db)]
    FA --> Sched[APScheduler<br/>· in-process]
    Sched -->|cron 6am| Agent[Fetch agent]
    Agent -->|HTTP| Apify1[Apify · profile-posts]
    Agent -->|HTTP| Apify2[Apify · profile-scraper]
    Agent -->|subprocess| Claude[Claude CLI]
    FA -->|render| UI["Jinja2 + HTMX templates"]
```

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | One-process app with built-in scheduler |
| Frontend | Jinja2 + HTMX | Server-rendered, no React build pipeline |
| Database | SQLite (raw `aiosqlite`) | Single file, zero ops, sub-ms queries |
| Scheduler | APScheduler | In-process; no Redis/Celery |
| LLM | Claude CLI (subscription) | No per-call API cost |
| LinkedIn data | Apify actors | Read-only, no cookies |

The entire stack runs in one Python process against one `.db` file on a laptop.

---

## Data model (simplified)

```mermaid
erDiagram
    handles ||--o{ posts : "has"
    handles ||--o{ handle_tags : "tagged with"
    tags    ||--o{ handle_tags : ""
    posts   ||--o{ generated_comments : "drafts"
    posts   ||--o{ posted_log : "posted as"
    generated_comments ||--o| posted_log : "becomes"

    handles {
        int id
        string linkedin_handle
        string display_name
        bool active
        text notes
        json enrichment_json
        datetime enriched_at
        datetime last_fetched_at
    }
    tags {
        int id
        string slug
        string label
        string dimension  "persona|reach|intent|cadence"
    }
    posts {
        int id
        int handle_id
        string post_id
        text content
        datetime posted_at
        string status
    }
    generated_comments {
        int id
        int post_id
        string tone
        text content
    }
    posted_log {
        int id
        int post_id
        int comment_id
        string tone
        int rating "1-5"
        datetime posted_at
    }
```

---

## Talking points for an executive audience

| Question they'll ask | One-line answer |
|---|---|
| *What does this actually replace?* | The 60–90 minutes a day of scrolling, drafting, second-guessing comments. |
| *Isn't this just ChatGPT for LinkedIn?* | No — the value is in *curation* (handle watch list), *structure* (4-dimension tagging), and *prioritisation* (scored activation). The drafting is the easy part. |
| *Does it post for me?* | Never. Copy-paste only. Read-only on LinkedIn. |
| *Why six tones?* | Prevents AI tone-collapse and matches the relationship to the comment. |
| *How does it decide who's worth engaging today?* | Transparent scoring — every "+/−" is visible. You can override. |
| *Why a personal tool, not SaaS?* | The watch list and intent tags are *strategy* — they shouldn't leave your laptop. |

---

## Suggested diagram-generation prompts

Paste this file into Claude or ChatGPT with prompts like:

1. *"Render the end-to-end workflow as a single executive-friendly swimlane diagram with three lanes: System, Apify+Claude, Human."*
2. *"Turn the activation-scoring section into a one-page decision flowchart with the recency cooldown branches expanded."*
3. *"Generate a 1-slide summary illustrating the daily 5-minute loop, optimised for a non-technical audience."*
4. *"Convert the feature map into a 2×2 matrix of (Effort × Strategic Value), placing each feature in a quadrant."*

---

## Demo script — 90 seconds

1. **Open `/admin`** — "Here are the 26 people I track. Each has structure: persona, reach, intent, cadence."
2. **Hover the *Recommended to activate* panel** — "App scored these dormant handles. Score 11 on top: `+3 prospect, +5 posts:4, +2 reach-large, −1 posted 5d ago`. Math is on screen."
3. **Open `/dashboard`** — "This morning's posts. Each post, six comment options. I pick, edit if needed, copy."
4. **Paste into LinkedIn, mark posted, rate ★4** — "Done. 30 seconds per post. Ratings train my own sense of which tones land."
5. **Open `/posted`** — "Historical view. I can see which handles I'm engaging with too much, too little, and which tones rate best."
