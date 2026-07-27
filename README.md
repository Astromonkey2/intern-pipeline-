# intern-pipeline

Finds internships and files a daily digest as a GitHub Issue.

Pulls from public listing repos, then **discovers every Ashby / Greenhouse / Lever board
those listings reference** and polls each one directly — currently 270 boards, growing on
its own as new companies appear. That's the point: it catches roles days before the
aggregator repos do, and nothing is capped by a hand-typed company list.

Runs at **13:00 UTC daily**. New roles → an Issue → GitHub emails you. Nothing new → silence.

---

## Use it for your own search (fork it)

Nothing here is specific to hardware except one file. To point it at *your* field:

1. **Fork** this repo (top-right on GitHub).
2. **Pick an interest profile.** [`profiles/`](profiles/) has ready-made ones —
   `software-generic.json`, `data-ml.json`, `hardware.json`, `engineering-broad.json`
   (all disciplines: mechanical, aerospace, electrical, controls, industrial, …). Copy the
   one you want over [`profile.json`](profile.json) (open it, paste, commit — all on
   github.com). Or edit `profile.json` directly; see
   [Your interest profile](#your-interest-profile) below.
3. **Enable Actions** on your fork (the *Actions* tab → enable workflows). That's it — it
   runs on the daily schedule and files each digest as an Issue on your fork, which GitHub
   emails to you.

No secrets, no server, no mail setup. If `profile.json` is missing or broken, it falls back
to a broad "any software internship" profile, so a fresh fork still works out of the box.

**Known limits (honest):** location matching is US-centric — see [`location`](#location)
for how to widen or repoint it. And the reach is only as broad as the boards it discovers:
software, electrical, and computer-hardware roles are plentiful; mechanical, aerospace,
controls, and industrial show up in smaller but real numbers; **chemical, civil, and
materials are thin** — those employers mostly run Workday/custom portals this pipeline can't
poll, not Ashby/Greenhouse/Lever. Add sources in `SOURCES_BY_ROLE_TYPE` (JSON repos) or
`MARKDOWN_SEEDS` (README-table repos, used only to discover more boards) to widen it.

---

## Your interest profile

[`profile.json`](profile.json) is *what you're looking for* — the only file that decides
relevance. It has three parts:

**`categories`** — an ordered list of sections. The **first** category whose keywords appear
in a job title wins, so list the most specific first. Each is:

```json
{ "key": "backend",
  "label": "🔧 Backend / Distributed Systems",
  "keywords": ["backend", "distributed", "microservice", "api"] }
```

`key` is the id that `config.json`'s on/off toggles use. `label` is the section header in the
digest. `keywords` are matched case-insensitively anywhere in the title. Keep the last
category a broad catch-all so decent roles aren't dropped just for odd phrasing.

**`exclude_titles`** — a kill-list for noise that sneaks in under a good company (`"sales"`,
`"recruiting"`). Matched at word start, so `"account"` also kills `"accounting"`.

**`upstream_fallback`** *(optional, `null` to disable)* — if a source tags a role with a
category you care about (e.g. `"Hardware"`) but the title has no keyword hit, file it in the
`into` bucket instead of dropping it: `{ "match": "hardware", "into": "hardware_other" }`.

---

## Customising it

Edit [`config.json`](config.json) — **directly on github.com** (open the file, click the
pencil, commit). The next run picks it up. Any key you delete falls back to its default.

### `role_type`

| value | what you get |
|---|---|
| `"intern"` | internships and co-ops only *(default)* |
| `"new_grad"` | entry-level full-time; senior/staff/lead titles filtered out |
| `"both"` | both, minus senior titles |

Switching also swaps which source repos get pulled.

### `location`

```json
"location": { "mode": "us_only", "extra_allow": [], "extra_block": [] }
```

`"us_only"` (default) or `"any"`.

The matching is deliberately lopsided: a **US signal keeps** the role, a **non-US signal
drops** it, and anything **unrecognised is kept**. Losing a real US role is worse than
letting one foreign posting through, and location strings are a mess — `"SF"`,
`"Austin, Texas, United States"`, `"North America"`, `""` are all real values.

Add your own substrings if something slips through:

```json
"extra_block": ["belgrade", "remote - emea"],
"extra_allow": ["ann arbor", "college station"]
```

### `categories`

Toggles for the sections defined in your [`profile.json`](#your-interest-profile) — the keys
here must match the category `key`s there. Any key you omit defaults to on. Set one to
`false` to hide that section entirely. For the default hardware profile:

```json
"categories": {
  "silicon": true,           // RTL, ASIC, DV, physical design, tapeout
  "architecture": true,      // microarchitecture, accelerators, GPU/CPU perf
  "embedded": true,          // firmware, RTOS, drivers, board bring-up
  "ml_systems": true,        // compilers, kernels, CUDA/MLIR, inference
  "hardware_other": true,    // PCB, RF, thermal, test/validation
  "software_general": true   // everything else that passes relevance
}
```

`software_general` is the biggest bucket by far (~two-thirds). Turning it off gives you a
short, dense, hardware-only digest.

### `priority_companies`

Cosmetic only — these get a ⭐ and sort to the top of their section. **Being absent never
excludes anyone.** Matched on whole words, so `arm` doesn't match Armstrong.

### `extra_exclude_titles`

Added to the built-in kill-list. Matched at word start, so `"account"` also kills
`"accounting"`. Use it when noise keeps showing up:

```json
"extra_exclude_titles": ["game", "salesforce", "consulting"]
```

### `max_roles_per_issue`

Default 300. GitHub hard-caps an issue body at 65,536 characters. Overflow is **not**
marked as seen, so it leads the next digest rather than being lost.

---

## Running it yourself

```bash
python intern_pipeline.py --dry-run                # writes digest.html, changes nothing
python intern_pipeline.py --dry-run --ignore-seen  # ...and includes roles already shown
python intern_pipeline.py --issue                  # stages digest.md + pending_ids.json
python intern_pipeline.py --seed                   # mark everything currently live as seen
python intern_pipeline.py --no-ats                 # skip board polling (fast, repos only)
```

`--dry-run` still respects the seen-list, so on its own it only shows what's new. Add
`--ignore-seen` to see **every** matching role — that's what you want for browsing the
backlog or checking how a `config.json` change affects the full corpus. It's refused
outside `--dry-run`, since re-delivering the whole backlog to your inbox is never intended.
Open the resulting `digest.html` in a browser.

## State files (don't hand-edit)

| file | what it is |
|---|---|
| `seen_ids.json` | roles already shown to you — how repeats are avoided |
| `ats_boards.json` | discovered job boards; grows every run, never shrinks |
| `pending_ids.json` | staged mid-run; promoted to `seen` only after the Issue is filed |

That last one is why a failed issue-creation doesn't lose roles: nothing is marked seen
until delivery is confirmed.
