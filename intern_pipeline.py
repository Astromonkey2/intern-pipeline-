#!/usr/bin/env python3
"""
intern_pipeline.py  —  Abhi's internship aggregator

WHAT IT DOES
  1. Pulls structured listings from public intern-listing repos (the "seeds").
  2. DISCOVERS every Ashby / Greenhouse / Lever board referenced by those listings,
     remembers them in ats_boards.json, and polls each one directly.
  3. Classifies roles into categories by what the work actually is.
  4. Dedupes (by id, and by company+title so one role posted 3× shows once).
  5. Delivers a digest of ONLY new matches, by email or as a GitHub Issue.

WHY STEP 2 MATTERS
  Listing repos lag their sources — Etched's RTL Intern was live on Ashby days before
  it appeared in any repo. So the repos are used for two things: the roles themselves,
  AND as a directory of which boards exist. Every new company that shows up in a repo
  permanently adds its board to the poll list. The reach compounds; nothing is capped
  by a list someone typed by hand.

RUN
  python3 intern_pipeline.py --dry-run     # writes digest.html, delivers nothing
  python3 intern_pipeline.py               # sends the email (needs SMTP env vars)
  python3 intern_pipeline.py --issue       # writes digest.md + pending_ids.json
  python3 intern_pipeline.py --commit-seen # folds pending_ids.json into seen_ids.json

ISSUE MODE IS TWO-PHASE, ON PURPOSE
  --issue only *stages* the new ids in pending_ids.json; it does not mark them seen.
  The caller (the GitHub workflow) files the issue, and only once that succeeds calls
  --commit-seen. If issue creation fails, nothing is marked seen and the next run
  retries those roles instead of losing them forever.
"""

import argparse, json, os, re, smtplib, ssl, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ────────────────────────────── SEED SOURCES ──────────────────────────────
# These are SEEDS, not the boundary of the search. Their real job is to keep
# feeding new ATS boards into ats_boards.json. Add repos freely.

LISTING_SOURCES = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
]

# ────────────────────────────── RELEVANCE ──────────────────────────────
# Categories are ordered: the first one whose keywords hit wins, so put the
# most specific first. Retitle/reorder these to reshape the digest.

CATEGORIES = [
    ("🔬 Silicon · RTL & Verification", [
        "rtl", "asic", "fpga", "verilog", "systemverilog", "vlsi", "physical design",
        "design verification", "dft", "synthesis", "static timing", "tapeout", "layout",
        "analog", "mixed signal", "circuit", "semiconductor", "chip design", "silicon",
        "pd intern", "dv intern", "sta intern",   # the abbreviations boards actually use
    ]),
    ("🧠 Computer Architecture & Accelerators", [
        "architecture", "architect", "microarchitect", "soc", "accelerator", "gpu",
        "npu", "tpu", "cpu", "performance model", "chip simulation", "chipsim",
        "memory subsystem", "interconnect", "computer engineering",
    ]),
    ("⚡ Embedded & Firmware", [
        "embedded", "firmware", "bare metal", "rtos", "device driver", "driver",
        "bsp", "board bring", "hardware software", "bringup", "bring-up",
    ]),
    ("🛠️ ML Systems · Compilers & Kernels", [
        "compiler", "kernel", "cuda", "triton", "mlir", "llvm", "inference",
        "runtime", "hpc", "high performance", "distributed training", "ml systems",
        "systems software", "operating system", "performance engineer",
    ]),
    ("🔌 Other hardware & systems", [
        "hardware engineer", "electrical engineer", "power electronics", "pcb",
        "signal integrity", "test engineer", "validation", "thermal", "rf ",
        "radio frequency", "sensor", "optical", "photonic", "failure analysis",
    ]),
    ("💻 Software & ML (general)", [
        "software", "machine learning", "deep learning", "artificial intelligence",
        "computer vision", "robotics", "backend", "platform", "infrastructure",
        "systems", "data engineer", "research engineer", "autonomy",
    ]),
]

HARDWARE_FALLBACK_CATEGORY = "🔌 Other hardware & systems"

# Upstream `category` values that are never relevant, whatever the title says.
EXCLUDE_UPSTREAM_CATEGORIES = {"quant", "quantitative finance", "product", "product management"}

# Title kill-list. Catches non-technical roles that slip in under a good category
# (e.g. Etched's "GTM Intern" / "Talent Intern" arriving under a hardware company).
EXCLUDE_TITLE = [
    "sales", "marketing", "recruit", "talent", "gtm", "go-to-market", "finance",
    "accounting", "human resource", "hr", "legal", "counsel", "social work",
    "nurse", "clinical", "teacher", "tutor", "customer success", "business develop",
    "supply chain", "operations intern", "core ops",
    "communication", "public relations", "people ops",
    "phd",              # Abhi is an undergrad — PhD-gated research roles aren't open to him
    "mba", "medical",
]

def _prefix_re(terms):
    """Match at a word start, but allow suffixes: 'recruit' catches recruiting AND
    recruiter, 'counsel' catches counseling. Without the left boundary, 'hr' would
    fire on 'thread' and 'intern' on 'international'."""
    return re.compile(r'(?<![a-z0-9])(?:' + "|".join(re.escape(t) for t in terms) + r')')

def _word_re(terms):
    """Both boundaries. Substring matching starred Armstrong and Charm as 'arm',
    Intelligent as 'intel', Metabolon as 'meta', and Amdocs as 'amd'."""
    return re.compile(r'(?<![a-z0-9])(?:' + "|".join(re.escape(t) for t in terms)
                      + r')(?![a-z0-9])')

_EXCLUDE_RE = _prefix_re(EXCLUDE_TITLE)

# ATS tokens are careers-portal slugs, not company names: "tenstorrentuniversity",
# "asteraearlycareer2026". Strip the recruiting cruft so the digest reads properly
# and so dedupe can match these against the repos' clean company names.
_SLUG_CRUFT = re.compile(
    r'(?i)[-_ ]?(university|earlycareers?|earlycareer|campus|careers?|jobs|'
    r'internships?|interns?|talent|recruiting|hiring|students?)\d*$')

def pretty_company(token):
    s = token.replace("-", " ").replace("_", " ").strip()
    for _ in range(3):                       # "astera early career 2026" needs a few passes
        s = _SLUG_CRUFT.sub("", s)
        s = re.sub(r'\s*\d{4}$', "", s).strip()
    return s.title() if s.islower() or s.isupper() else s

# Optional flavour only — these get a ⭐ and sort to the top of their category.
# Being absent from this list NEVER excludes a company.
PRIORITY_COMPANIES = [
    "nvidia", "amd", "groq", "cerebras", "tenstorrent", "etched", "qualcomm",
    "apple", "intel", "broadcom", "micron", "sambanova", "ambarella", "arm",
    "google", "meta", "microsoft", "tesla", "anthropic", "openai", "waymo",
]

_PRIORITY_RE = _word_re(PRIORITY_COMPANIES)

# GitHub hard-caps an issue body at 65,536 chars; a role line runs ~155. 300 keeps us
# well clear. Overflow is NOT marked seen, so it simply lands in tomorrow's digest.
MAX_ROLES_PER_ISSUE = 300

MUST_CONTAIN_INTERN = True   # Abhi is a student, not a new grad — internships only.
MAX_BOARD_WORKERS = 16       # parallel ATS polls; be a decent citizen

SEEN_FILE = "seen_ids.json"
PENDING_FILE = "pending_ids.json"
BOARDS_FILE = "ats_boards.json"   # the discovered-board memory; grows, never shrinks
UA = {"User-Agent": "intern-pipeline/2.0 (personal job tracker)"}

# ────────────────────────────── FETCHERS ──────────────────────────────

def _get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fetch_listing_repo(url):
    try:
        data = json.loads(_get(url, timeout=45))
    except Exception as e:
        print(f"  ! repo fetch failed ({url.split('/')[4]}): {e}", file=sys.stderr)
        return []
    out = []
    for d in data:
        if not (d.get("active") and d.get("is_visible")):
            continue
        out.append({
            "id": f"gh:{d.get('id')}",
            "company": (d.get("company_name") or "").strip(),
            "title": (d.get("title") or "").strip(),
            "url": d.get("url", ""),
            "locations": d.get("locations") or [],
            "terms": d.get("terms") or ([d["season"]] if d.get("season") else []),
            "upstream_category": (d.get("category") or "").strip(),
            "date_posted": d.get("date_posted"),
            "src": "repo",
        })
    return out

def fetch_ashby(token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    return [{
        "id": f"ashby:{j.get('id')}", "company": pretty_company(token),
        "title": (j.get("title") or "").strip(), "url": j.get("jobUrl", ""),
        "locations": [j["location"]] if j.get("location") else [],
        "terms": [], "upstream_category": "", "date_posted": None, "src": "ats",
    } for j in json.loads(_get(url)).get("jobs", [])]

def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    return [{
        "id": f"gh_ats:{j.get('id')}", "company": pretty_company(token),
        "title": (j.get("title") or "").strip(), "url": j.get("absolute_url", ""),
        "locations": [(j.get("location") or {}).get("name", "")],
        "terms": [], "upstream_category": "", "date_posted": None, "src": "ats",
    } for j in json.loads(_get(url)).get("jobs", [])]

def fetch_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    return [{
        "id": f"lever:{j.get('id')}", "company": pretty_company(token),
        "title": (j.get("text") or "").strip(), "url": j.get("hostedUrl", ""),
        "locations": [(j.get("categories") or {}).get("location", "")],
        "terms": [], "upstream_category": "", "date_posted": None, "src": "ats",
    } for j in json.loads(_get(url))]

ATS_DISPATCH = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever}

# ────────────────────────────── BOARD DISCOVERY ──────────────────────────────

ATS_URL_PATTERNS = {
    "ashby":      r'(?:jobs\.)?ashbyhq\.com/([^/?#]+)',
    "greenhouse": r'(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)',
    "lever":      r'jobs\.lever\.co/([^/?#]+)',
}

def discover_boards(roles, known):
    """Mine ATS tokens out of listing URLs. Case is preserved — Ashby tokens are
    case-sensitive in the URL path — but membership is compared case-insensitively."""
    found = 0
    for r in roles:
        for kind, pat in ATS_URL_PATTERNS.items():
            m = re.search(pat, r.get("url") or "")
            if not m:
                continue
            token = m.group(1)
            if token.lower() not in {t.lower() for t in known.get(kind, [])}:
                known.setdefault(kind, []).append(token)
                found += 1
    return found

def load_boards():
    if os.path.exists(BOARDS_FILE):
        return json.load(open(BOARDS_FILE, encoding="utf-8"))
    return {}

def save_boards(boards):
    json.dump({k: sorted(set(v)) for k, v in boards.items()},
              open(BOARDS_FILE, "w", encoding="utf-8"), indent=1)

def poll_boards(boards):
    """Hit every known board in parallel. Individual failures are expected and
    ignored — boards go private, get renamed, or rate-limit."""
    jobs = [(kind, tok) for kind, toks in boards.items() for tok in toks]
    roles, dead = [], 0
    def one(job):
        kind, tok = job
        try:
            return ATS_DISPATCH[kind](tok)
        except Exception:
            return None
    with ThreadPoolExecutor(MAX_BOARD_WORKERS) as ex:
        for res in ex.map(one, jobs):
            if res is None:
                dead += 1
            else:
                roles += res
    print(f"polled {len(jobs)} ATS boards -> {len(roles)} postings ({dead} unreachable)")
    return roles

# ────────────────────────────── FILTER + CLASSIFY ──────────────────────────────

SEASON_START_MONTH = {"winter": 1, "spring": 3, "summer": 5, "fall": 9, "autumn": 9}

def term_is_current(terms):
    """True if any term plausibly hasn't started yet. Derived from today's date, so
    this never needs editing as cycles roll over. Unknown/absent terms pass —
    ATS postings carry no term and we'd rather over-include than miss a role."""
    if not terms:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for t in terms:
        m = re.search(r'(winter|spring|summer|fall|autumn)\D*(\d{4})', str(t), re.I)
        if not m:
            return True                       # "N/A" and friends — don't guess, keep it
        start = datetime(int(m.group(2)), SEASON_START_MONTH[m.group(1).lower()], 1,
                         tzinfo=timezone.utc)
        if start >= cutoff:
            return True
    return False

def classify(role):
    """Return a category name, or None to drop the role."""
    t = role["title"].lower()
    if MUST_CONTAIN_INTERN and "intern" not in t:
        return None
    if _EXCLUDE_RE.search(t):
        return None
    if role.get("upstream_category", "").lower() in EXCLUDE_UPSTREAM_CATEGORIES:
        return None
    if not term_is_current(role.get("terms")):
        return None
    for name, keywords in CATEGORIES:
        if any(k in t for k in keywords):
            return name
    # Upstream said Hardware but the title didn't match our vocabulary. Keep it, but
    # in the catch-all bucket — routing these into Silicon buried the real RTL roles
    # under Tesla's solar/thermal/HVAC hardware postings.
    if "hardware" in role.get("upstream_category", "").lower():
        return HARDWARE_FALLBACK_CATEGORY
    return None

def norm_title(s):
    """Collapse cosmetic variants so one role posted per-location shows once."""
    s = re.sub(r'\(.*?\)|\[.*?\]', ' ', s.lower())
    s = re.sub(r'\b(20\d\d|summer|fall|winter|spring|intern|internship|co-?op)\b', ' ', s)
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()

def norm_url(u):
    """Same posting reached via a repo and via its ATS differs only in case and in
    trailing /application — that's one job, not two."""
    u = (u or "").lower().split("?")[0].rstrip("/")
    return u.removesuffix("/application")

def norm_company(c):
    c = c.strip().lower()
    for suf in (".ai", ".com", " inc", " inc.", " llc", " corp", " corporation", " ltd"):
        c = c.removesuffix(suf)
    return c.strip()

def dedupe(roles):
    """Three passes, cheapest first: exact id, then canonical URL (catches the same
    posting arriving from a repo AND from its own ATS), then company+normalized-title
    (catches Etched's 'Firmware Intern' ×3 — three postings, one job to apply to).
    Repo entries are fetched first and therefore win, which is what we want: they
    carry clean company names and term data that ATS postings lack."""
    by_id, out, seen_urls, seen_pairs = {}, [], set(), set()
    for r in roles:
        by_id.setdefault(r["id"], r)
    for r in by_id.values():
        u = norm_url(r.get("url"))
        if u and u in seen_urls:
            continue
        key = (norm_company(r["company"]), norm_title(r["title"]))
        if key in seen_pairs:
            continue
        seen_urls.add(u)
        seen_pairs.add(key)
        out.append(r)
    return out

def is_priority(role):
    return bool(_PRIORITY_RE.search(role["company"].lower()))

# ────────────────────────────── DIGEST ──────────────────────────────

def _sorted(roles):
    return sorted(roles, key=lambda r: (not is_priority(r), r["company"].lower()))

def _line_parts(r):
    loc = ", ".join([l for l in r["locations"] if l][:2]) or "—"
    star = "⭐ " if is_priority(r) else ""
    return star, loc

def cap_for_issue(by_cat):
    """Trim to MAX_ROLES_PER_ISSUE, keeping category order (most relevant first) and
    starred companies within each. Returns the trimmed map, the ids that actually made
    it into the body, and the overflow count. Only the included ids get staged — the
    rest stay unseen on purpose so they resurface tomorrow rather than being lost."""
    total = sum(len(v) for v in by_cat.values())
    if total <= MAX_ROLES_PER_ISSUE:
        return by_cat, {r["id"] for v in by_cat.values() for r in v}, 0
    out, ids, budget = {}, set(), MAX_ROLES_PER_ISSUE
    for name, _ in CATEGORIES:
        if budget <= 0:
            break
        roles = _sorted(by_cat.get(name, []))[:budget]
        if roles:
            out[name] = roles
            ids |= {r["id"] for r in roles}
            budget -= len(roles)
    return out, ids, total - len(ids)

def build_digest_md(by_cat, overflow=0):
    now = datetime.now(timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p")
    total = sum(len(v) for v in by_cat.values())
    lines = [f"**{total} new** · _{now}_", ""]
    for name, _ in CATEGORIES:
        roles = by_cat.get(name, [])
        if not roles:
            continue
        lines += [f"## {name} ({len(roles)})", ""]
        for r in _sorted(roles):
            star, loc = _line_parts(r)
            lines.append(f"- {star}**[{r['title']}]({r['url']})** — {r['company']} · {loc}")
        lines.append("")
    if not total:
        lines.append("No new roles this run.")
    if overflow:
        lines += [f"> **{overflow} more** didn't fit GitHub's issue size limit. "
                  f"They're still marked unseen and will lead tomorrow's digest.", ""]
    lines += ["---", "⭐ = a company you're tracking. Boards say a role went live — you "
              "still apply on the company site. Log it in your tracker."]
    return "\n".join(lines)

def build_digest(by_cat):
    now = datetime.now(timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p")
    total = sum(len(v) for v in by_cat.values())
    rows = []
    for name, _ in CATEGORIES:
        roles = by_cat.get(name, [])
        if not roles:
            continue
        rows.append(f'<h2 style="margin:22px 0 6px;font-size:16px;">{name} '
                    f'<span style="color:#888;font-weight:400;">({len(roles)})</span></h2>')
        for r in _sorted(roles):
            star, loc = _line_parts(r)
            rows.append(
                f'<div style="padding:8px 0;border-bottom:1px solid #eee;">'
                f'<a href="{r["url"]}" style="font-weight:600;color:#1a5;text-decoration:none;">'
                f'{star}{r["title"]}</a>'
                f'<div style="color:#555;font-size:13px;">{r["company"]} · {loc}</div></div>')
    body = "".join(rows) or "<p>No new roles this run.</p>"
    return f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;">
      <h1 style="font-size:20px;margin:0;">Internship digest · {total} new</h1>
      <div style="color:#888;font-size:12px;margin:2px 0 10px;">{now}</div>
      {body}
      <p style="color:#aaa;font-size:11px;margin-top:20px;">
      ⭐ = a company you're tracking. Boards say a role went live — you still apply on
      the company site.</p></div>"""

# ────────────────────────────── STATE + SEND ──────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        return set(json.load(open(SEEN_FILE, encoding="utf-8")))
    return set()

def save_seen(ids):
    json.dump(sorted(ids), open(SEEN_FILE, "w", encoding="utf-8"))

def commit_seen():
    """Phase 2 of issue mode: promote staged ids once delivery is confirmed."""
    if not os.path.exists(PENDING_FILE):
        print("no pending_ids.json — nothing to promote")
        return
    pending = set(json.load(open(PENDING_FILE, encoding="utf-8")))
    merged = load_seen() | pending
    save_seen(merged)
    os.remove(PENDING_FILE)
    print(f"promoted {len(pending)} ids to seen ({len(merged)} total)")

def send_email(html):
    host = os.environ["SMTP_HOST"]; port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ["SMTP_USER"]; pw = os.environ["SMTP_PASS"]
    to = os.environ.get("DIGEST_TO", user)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🗂️ Internship digest"
    msg["From"] = user; msg["To"] = to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())

# ────────────────────────────── MAIN ──────────────────────────────

def main():
    # Windows consoles default to cp1252 and blow up on the category emoji.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write digest.html, deliver nothing")
    ap.add_argument("--issue", action="store_true",
                    help="stage digest.md + pending_ids.json for GitHub Issue delivery")
    ap.add_argument("--commit-seen", action="store_true",
                    help="promote pending_ids.json into seen_ids.json after delivery succeeded")
    ap.add_argument("--no-ats", action="store_true", help="skip ATS polling (fast, repos only)")
    ap.add_argument("--seed", action="store_true",
                    help="one-time backfill: write digest.html as a browsable backlog and "
                         "mark everything currently live as seen, so the next real digest "
                         "contains only genuinely new roles")
    args = ap.parse_args()

    if args.commit_seen:
        commit_seen()
        return

    roles = []
    for url in LISTING_SOURCES:
        roles += fetch_listing_repo(url)
    print(f"pulled {len(roles)} active roles from {len(LISTING_SOURCES)} seed repo(s)")

    boards = load_boards()
    added = discover_boards(roles, boards)
    save_boards(boards)
    print(f"known ATS boards: {sum(len(v) for v in boards.values())} (+{added} new this run)")

    if not args.no_ats:
        roles += poll_boards(boards)

    by_cat, fresh_ids = {}, set()
    seen = load_seen()
    kept = dedupe(roles)
    for r in kept:
        cat = classify(r)
        if cat is None or r["id"] in seen:
            continue
        by_cat.setdefault(cat, []).append(r)
        fresh_ids.add(r["id"])

    total = sum(len(v) for v in by_cat.values())
    print(f"{len(kept)} after dedupe -> {total} NEW relevant roles")
    for name, _ in CATEGORIES:
        if by_cat.get(name):
            print(f"    {len(by_cat[name]):4d}  {name}")

    if args.dry_run:
        open("digest.html", "w", encoding="utf-8").write(build_digest(by_cat))
        print("wrote digest.html (dry run — nothing sent, seen-list untouched)")
        return

    if args.seed:
        open("digest.html", "w", encoding="utf-8").write(build_digest(by_cat))
        save_seen(seen | fresh_ids)
        print(f"seeded {len(fresh_ids)} ids as seen; the full backlog is in digest.html "
              f"for you to browse. The next digest will only carry new roles.")
        return

    if args.issue:
        if not total:
            print("nothing new — no issue to file")
            return
        by_cat, fresh_ids, overflow = cap_for_issue(by_cat)
        if overflow:
            print(f"  capped at {MAX_ROLES_PER_ISSUE} roles for this issue; "
                  f"{overflow} deferred to the next run (deliberately left unseen)")
        open("digest.md", "w", encoding="utf-8").write(build_digest_md(by_cat, overflow))
        json.dump(sorted(fresh_ids), open(PENDING_FILE, "w", encoding="utf-8"))
        print(f"staged digest.md + {len(fresh_ids)} pending ids "
              f"(not yet seen — caller must confirm delivery with --commit-seen)")
        return

    if total:
        send_email(build_digest(by_cat))
        save_seen(seen | fresh_ids)
        print(f"sent digest, recorded {len(fresh_ids)} ids as seen")
    else:
        print("nothing new — no email sent")

if __name__ == "__main__":
    main()
