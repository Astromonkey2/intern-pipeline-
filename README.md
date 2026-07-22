# intern-pipeline

Finds internships and files a daily digest as a GitHub Issue.

Pulls from public listing repos, then **discovers every Ashby / Greenhouse / Lever board
those listings reference** and polls each one directly — currently 270 boards, growing on
its own as new companies appear. That's the point: it catches roles days before the
aggregator repos do, and nothing is capped by a hand-typed company list.

Runs at **13:00 UTC daily**. New roles → an Issue → GitHub emails you. Nothing new → silence.

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

Set any to `false` to hide that section entirely:

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
