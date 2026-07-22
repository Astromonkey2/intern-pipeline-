#!/usr/bin/env python3
"""
intern_pipeline.py  —  Abhi's internship aggregator

WHAT IT DOES
  1. Pulls structured listings from GitHub intern repos (vanshb03 / SimplifyJobs).
  2. Pulls directly from your TARGET companies' ATS boards (Ashby / Greenhouse / Lever).
  3. Tiers + filters roles: [1] target companies  [2] hardware/RTL  [3] general SWE.
  4. Dedupes against roles it has already shown you (seen_ids.json).
  5. Delivers a digest of ONLY new matches, by email or as a GitHub Issue.

DESIGN NOTE
  You do NOT scrape LinkedIn / Indeed / Handshake here — those block bots and forbid it.
  Turn on their native email alerts and let a Gmail filter label them. This engine only
  touches sources that publish machine-readable data.

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

import argparse, json, os, smtplib, ssl, sys, time, urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ────────────────────────────── CONFIG (edit freely) ──────────────────────────────

TARGET_COMPANIES = [   # roles from these ALWAYS surface, whatever the title
    "nvidia", "amd", "groq", "cerebras", "tenstorrent", "etched",
    "qualcomm", "apple", "intel", "broadcom", "micron", "sambanova",
]

HARDWARE_KEYWORDS = [  # Tier 2: hardware / RTL relevance
    "rtl", "asic", "fpga", "verilog", "systemverilog", "vlsi", "silicon",
    "hardware", "architect", "microarchitect", "physical design", "verification",
    "soc", "digital design", "chip", "accelerator", "computer architecture",
]

GENERAL_KEYWORDS = [   # Tier 3: keep as a fallback net; set to [] to turn off
    "software engineer", "machine learning", "ml ", "systems", "compiler", "embedded",
]

EXCLUDE_KEYWORDS = [   # kill obvious noise regardless of tier
    "sales", "marketing", "recruit", "finance intern", "accounting", "hr ",
]

SEASONS = {"Summer", "Fall"}          # which cohorts you care about right now
MUST_CONTAIN_INTERN = True            # title must say "intern"

# TARGET companies' ATS boards. token = the slug in their careers URL.
# type is one of: "ashby" | "greenhouse" | "lever". Add/verify tokens as you find them.
ATS_BOARDS = [
    {"company": "Etched", "type": "ashby", "token": "Etched"},   # proven working
    # {"company": "Groq",     "type": "greenhouse", "token": "groq"},
    # {"company": "Cerebras", "type": "lever",      "token": "cerebras"},
    # NVIDIA / AMD run on Workday (no clean public JSON) — catch those via the
    # GitHub repo + a LinkedIn job alert instead. Don't fight Workday here.
]

GITHUB_LISTINGS = [
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    # add SimplifyJobs new-grad repo here later if you want senior-year roles too
]

SEEN_FILE = "seen_ids.json"
PENDING_FILE = "pending_ids.json"   # staged ids, promoted to SEEN_FILE only after delivery
UA = {"User-Agent": "intern-pipeline/1.0 (personal job tracker)"}

# ────────────────────────────── FETCHERS ──────────────────────────────

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fetch_github(url):
    try:
        data = json.loads(_get(url))
    except Exception as e:
        print(f"  ! github fetch failed ({url.split('/')[3]}): {e}", file=sys.stderr)
        return []
    out = []
    for d in data:
        if not (d.get("active") and d.get("is_visible")):
            continue
        out.append({
            "id": f"gh:{d.get('id')}",
            "company": d.get("company_name", "").strip(),
            "title": d.get("title", "").strip(),
            "url": d.get("url", ""),
            "locations": d.get("locations") or [],
            "season": d.get("season", ""),
            "sponsorship": d.get("sponsorship", ""),
            "date_posted": d.get("date_posted"),
            "src": "github",
        })
    return out

def fetch_ashby(token, company):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    try:
        data = json.loads(_get(url))
    except Exception as e:
        print(f"  ! ashby fetch failed ({company}): {e}", file=sys.stderr)
        return []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": f"ashby:{j.get('id')}",
            "company": company,
            "title": (j.get("title") or "").strip(),
            "url": j.get("jobUrl", ""),
            "locations": [j.get("location")] if j.get("location") else [],
            "season": "", "sponsorship": "",
            "date_posted": None, "src": f"ats:{company}",
        })
    return out

def fetch_greenhouse(token, company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        data = json.loads(_get(url))
    except Exception as e:
        print(f"  ! greenhouse fetch failed ({company}): {e}", file=sys.stderr)
        return []
    return [{
        "id": f"gh_ats:{j.get('id')}", "company": company,
        "title": (j.get("title") or "").strip(), "url": j.get("absolute_url", ""),
        "locations": [j.get("location", {}).get("name", "")] if j.get("location") else [],
        "season": "", "sponsorship": "", "date_posted": None, "src": f"ats:{company}",
    } for j in data.get("jobs", [])]

def fetch_lever(token, company):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        data = json.loads(_get(url))
    except Exception as e:
        print(f"  ! lever fetch failed ({company}): {e}", file=sys.stderr)
        return []
    return [{
        "id": f"lever:{j.get('id')}", "company": company,
        "title": (j.get("text") or "").strip(), "url": j.get("hostedUrl", ""),
        "locations": [j.get("categories", {}).get("location", "")],
        "season": "", "sponsorship": "", "date_posted": None, "src": f"ats:{company}",
    } for j in data]

ATS_DISPATCH = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever}

# ────────────────────────────── FILTER + TIER ──────────────────────────────

def tier_of(role):
    t = role["title"].lower()
    c = role["company"].lower()
    if any(x in t for x in EXCLUDE_KEYWORDS):
        return None
    if MUST_CONTAIN_INTERN and "intern" not in t:
        return None
    if role["season"] and SEASONS and role["season"] not in SEASONS and role["src"] == "github":
        return None
    if any(x in c for x in TARGET_COMPANIES):
        return 1
    if any(k in t for k in HARDWARE_KEYWORDS):
        return 2
    if GENERAL_KEYWORDS and any(k in t for k in GENERAL_KEYWORDS):
        return 3
    return None

TIER_LABEL = {1: "🎯 Target companies", 2: "⚙️ Hardware / RTL", 3: "💻 General SWE"}

# ────────────────────────────── DIGEST ──────────────────────────────

def build_digest(new_by_tier):
    now = datetime.now(timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p")
    total = sum(len(v) for v in new_by_tier.values())
    rows = []
    for tier in (1, 2, 3):
        roles = new_by_tier.get(tier, [])
        if not roles:
            continue
        rows.append(f'<h2 style="margin:22px 0 6px;font-size:16px;">{TIER_LABEL[tier]} '
                    f'<span style="color:#888;font-weight:400;">({len(roles)})</span></h2>')
        for r in sorted(roles, key=lambda x: x["company"].lower()):
            loc = ", ".join([l for l in r["locations"] if l][:2]) or "—"
            spons = f' · {r["sponsorship"]}' if r.get("sponsorship") else ""
            rows.append(
                f'<div style="padding:8px 0;border-bottom:1px solid #eee;">'
                f'<a href="{r["url"]}" style="font-weight:600;color:#1a5;text-decoration:none;">{r["title"]}</a>'
                f'<div style="color:#555;font-size:13px;">{r["company"]} · {loc}{spons}</div></div>')
    body = "".join(rows) or "<p>No new roles this run.</p>"
    return f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;">
      <h1 style="font-size:20px;margin:0;">Internship digest · {total} new</h1>
      <div style="color:#888;font-size:12px;margin:2px 0 10px;">{now}</div>
      {body}
      <p style="color:#aaa;font-size:11px;margin-top:20px;">
      Boards say a role went live — you still apply on the company site. Log it in your tracker.</p></div>"""

def build_digest_md(new_by_tier):
    """Markdown twin of build_digest() — GitHub Issues strip inline styles, so the
    HTML version renders as an unreadable wall there."""
    now = datetime.now(timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p")
    lines = [f"_{now}_", ""]
    for tier in (1, 2, 3):
        roles = new_by_tier.get(tier, [])
        if not roles:
            continue
        lines += [f"## {TIER_LABEL[tier]} ({len(roles)})", ""]
        for r in sorted(roles, key=lambda x: x["company"].lower()):
            loc = ", ".join([l for l in r["locations"] if l][:2]) or "—"
            spons = f" · {r['sponsorship']}" if r.get("sponsorship") else ""
            lines.append(f"- **[{r['title']}]({r['url']})** — {r['company']} · {loc}{spons}")
        lines.append("")
    if not any(new_by_tier.values()):
        lines.append("No new roles this run.")
    lines += ["---",
              "Boards say a role went live — you still apply on the company site. "
              "Log it in your tracker."]
    return "\n".join(lines)

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write digest.html, deliver nothing")
    ap.add_argument("--issue", action="store_true",
                    help="stage digest.md + pending_ids.json for GitHub Issue delivery")
    ap.add_argument("--commit-seen", action="store_true",
                    help="promote pending_ids.json into seen_ids.json after delivery succeeded")
    args = ap.parse_args()

    if args.commit_seen:
        commit_seen()
        return

    roles = []
    for url in GITHUB_LISTINGS:
        roles += fetch_github(url)
    for b in ATS_BOARDS:
        fn = ATS_DISPATCH.get(b["type"])
        if fn:
            roles += fn(b["token"], b["company"])
    print(f"pulled {len(roles)} raw roles from {len(GITHUB_LISTINGS)} repo(s) + {len(ATS_BOARDS)} ATS board(s)")

    seen = load_seen()
    new_by_tier = {}
    fresh_ids = set()
    for r in roles:
        tier = tier_of(r)
        if tier is None or r["id"] in seen:
            continue
        new_by_tier.setdefault(tier, []).append(r)
        fresh_ids.add(r["id"])

    total = sum(len(v) for v in new_by_tier.values())
    print(f"{total} NEW matching roles "
          f"(T1={len(new_by_tier.get(1,[]))} T2={len(new_by_tier.get(2,[]))} T3={len(new_by_tier.get(3,[]))})")

    if args.dry_run:
        open("digest.html", "w", encoding="utf-8").write(build_digest(new_by_tier))
        print("wrote digest.html (dry run — nothing sent, seen-list untouched)")
        return

    if args.issue:
        if not total:
            print("nothing new — no issue to file")
            return
        open("digest.md", "w", encoding="utf-8").write(build_digest_md(new_by_tier))
        json.dump(sorted(fresh_ids), open(PENDING_FILE, "w", encoding="utf-8"))
        print(f"staged digest.md + {len(fresh_ids)} pending ids "
              f"(not yet seen — caller must confirm delivery with --commit-seen)")
        return

    html = build_digest(new_by_tier)
    if total:
        send_email(html)
        save_seen(seen | fresh_ids)
        print(f"sent digest, recorded {len(fresh_ids)} ids as seen")
    else:
        print("nothing new — no email sent")

if __name__ == "__main__":
    main()
