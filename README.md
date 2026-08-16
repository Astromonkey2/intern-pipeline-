# intern-pipeline

**Once a day it checks hundreds of company job boards, picks out the new internships that match what you want, and posts the list. easy peasy**

It posts the list as a **GitHub Issue** on your own copy of this repo. GitHub then sends you an **email notification** about that new Issue so the list lands in your inbox without this tool ever needing your email password or a mail server. (That email is just GitHub's normal notification; it's on by default for your own repos.)

---

## Set it up (about 2 minutes)

1. **Fork this repo** — the *Fork* button, top-right on GitHub. Now you have your own copy.
2. **Pick what you're looking for.** Open the [`profiles/`](profiles/) folder and choose the one closest to you:
   - `hardware.json` — chips, RTL, embedded, electrical
   - `engineering-broad.json` — mechanical, aerospace, civil, materials, electrical, etc.
   - `software-generic.json` — software, web, backend, ML
   - `data-ml.json` — data science, ML, analytics

   Copy its contents into [`profile.json`](profile.json) (open `profile.json` on github.com → pencil icon → paste → *Commit*).
3. **Turn on the daily run.** Go to the **Actions** tab and click the button to enable workflows.

Done. It now runs every day at 1:00 PM UTC and posts new matches as an Issue..... which GitHub emails to you.

> Not sure which profile? Skip step 2 — with no `profile.json` it defaults to general software internships.

---

## Change what you get

Everything is two files you edit right on github.com. No coding.

### `profile.json` — *what kind of jobs*

This is the list of topics you care about. Each section looks like this:

```json
{ "key": "backend",
  "label": "🔧 Backend Engineering",
  "keywords": ["backend", "distributed", "api", "microservice"] }
```

- **`keywords`** — if any of these words is in the job title, the job goes in this section.
- **`label`** — the heading you'll see in your email.
- Put your most specific topics first, and keep a broad one last so nothing good slips through.

You can also add **`exclude_titles`** — words that get a job thrown out (e.g. `"sales"`, `"recruiting"`).

### `config.json` — *settings*

| setting | what it does |
|---|---|
| `role_type` | `"intern"` (default), `"new_grad"`, or `"both"` |
| `location` | `"us_only"` (default) or `"any"`. US jobs are kept, clearly-foreign ones dropped, unclear ones kept |
| `needs_sponsorship` | `true` if you need visa sponsorship — hides jobs that say "no sponsorship", "US citizens only", or need a security clearance |
| `categories` | turn any section from your profile on/off, e.g. `"software_general": false` |
| `priority_companies` | companies here get a ⭐ and jump to the top (never hides anything) |
| `extra_exclude_titles` | extra words to block when junk keeps showing up |
| `max_roles_per_issue` | most jobs per email (default 300) |

Any setting you leave out just uses its default.

---

## Good to know

- ** coverage.** Software, electrical, and computer hardware jobs are plentiful(no one hires us tho :( ). Mechanical, aerospace, and industrial show up in smaller numbers. Chemical, civil, and materials are thin — those companies mostly don't use the job boards this tool reads.
- **can't promise a company sponsors visas** — only hide the ones that clearly say they don't.
- **Some files run themselves don't edit them:** `seen_ids.json` (what you've already been shown), `ats_boards.json` (the list of job boards it found), `pending_ids.json` (temporary). The daily run updates these on its own.

---

## Running it on your own computer (optional)

Most people never need this — the daily GitHub run does everything. But if you want to try it locally:

```bash
python intern_pipeline.py --dry-run                # make a preview, send nothing
python intern_pipeline.py --dry-run --ignore-seen  # preview EVERY match, even old ones
```

`--dry-run` writes a `digest.html` file you can open in your browser. It never sends anything or changes what you've been shown.
