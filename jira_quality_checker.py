#!/usr/bin/env python3
"""
Jira Quality Validator
======================
Rule: For project INI, for every Initiative in "Selected for Development":
  - Sum the originalEstimate of all non-Cancelled child issues.
  - Compare that sum to the Initiative's own originalEstimate.
  - If they do NOT match → send a Slack DM to Andrii Mytsyk.

Schedule: run this script every hour via launchd (see setup_scheduler.sh).
"""

import json
import base64
import random
import sys
import urllib.request
import urllib.parse
import urllib.error
import logging
from datetime import datetime
from typing import Optional, Set

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/jira_quality_checker.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
JIRA_URL   = "https://gmntc.atlassian.net"
JIRA_EMAIL = "andrii.mytsyk@gamingtec.com"
JIRA_TOKEN = (
    "ATATT3xFfGF0jlSqcqsESsb_1xJfHZYfgU_3mojyRuCPeYsdsZbBziyRNtBWgKbLUP1JPIurBxF9s"
    "jqvRPe2CMQs1cDDBHUvfGMxRWUtzDlRwEmdkM9Xj2mnh3heYEyQ8iMbQ9D1xMBtfg-iVLJOsR1W1"
    "kNoniz_CDJuCyx1WNBK77xAhz1FIgw=7CE3D068"
)
SLACK_TOKEN = (
    "xoxp-15457714790-4783302274355-10907792749216-"
    "eeaefef336dbc810a1811ac541a0600a"
)
# ── Slack config ───────────────────────────────────────────────────────────────
SLACK_CHANNEL = "b2b-dev-estimates"

# Jira display name (or unique fragment) → Slack user ID
# Key matching is case-insensitive substring: "Jahor" matches "Jahor Klimovich"
JIRA_SLACK_USER_MAP = {
    "Alexandra Yavorska": "U04T8LRLQ2Z",
    "Jahor":              "U087A01BJJU",
    "Tati":               "U06MEGXR82E",
}

# ── Quality rule config ─────────────────────────────────────────────────────────
EMOJIS = ["🫪", "🐝", "🫈"]

# Statuses that qualify a ticket as "freshly entering development".
# If the most recent transition INTO "Selected for Development" came from one of
# these, the ticket is new and should be checked.
# Any other previous status (e.g. "Testing in Progress", "In Progress", "on Hold")
# means the ticket was already in dev and returned after QA — skip it.
FRESH_PREVIOUS_STATUSES = {"backlog", "ready for refinement", "discovery"}


# ═══════════════════════════════════════════════════════════════════════════════
#  Jira helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _jira_auth_header() -> str:
    raw = f"{JIRA_EMAIL}:{JIRA_TOKEN}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def jira_get(path: str) -> dict:
    url = JIRA_URL + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": _jira_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error("Jira HTTP %s for %s: %s", e.code, url, body)
        raise


def jira_post(path: str, body: dict) -> dict:
    """POST to Jira REST API (used for search/jql and similar endpoints)."""
    url = JIRA_URL + path
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": _jira_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        log.error("Jira POST HTTP %s for %s: %s", e.code, url, body_err)
        raise


def jira_search(jql: str, fields: str, max_results: int = 200) -> list:
    """Return all issues matching jql via POST /search/jql, handling cursor pagination."""
    # fields param can be comma-separated string — convert to list
    fields_list = [f.strip() for f in fields.split(",")]
    issues = []
    next_page_token = None  # type: Optional[str]

    while True:
        body: dict = {
            "jql": jql,
            "fields": fields_list,
            "maxResults": min(max_results, 100),  # API cap per page
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = jira_post("/rest/api/3/search/jql", body)
        batch = data.get("issues", [])
        issues.extend(batch)

        # Stop if no more pages or we've reached the desired max
        if data.get("isLast", True) or not batch or len(issues) >= max_results:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return issues[:max_results]


def get_initiative_children(parent_key: str, subtask_keys: Set[str]) -> list:
    """
    Fetch child issues of an Initiative via two methods:
      1. JQL  parent = KEY
      2. Direct fetch of any subtask keys not already returned by JQL
    """
    children = []
    fields = "key,summary,status,issuetype,timeoriginalestimate"

    # Method 1: JQL parent
    try:
        batch = jira_search(f"parent = {parent_key}", fields)
        children.extend(batch)
        log.info("  JQL parent=%s returned %d child issue(s)", parent_key, len(batch))
    except Exception as exc:
        log.warning("  JQL parent= failed for %s: %s", parent_key, exc)

    # Method 2: explicit subtask keys not already covered
    found_keys = {c["key"] for c in children}
    for st_key in subtask_keys - found_keys:
        try:
            issue = jira_get(f"/rest/api/3/issue/{st_key}?fields={fields}")
            children.append(issue)
            log.info("  Fetched subtask %s directly", st_key)
        except Exception as exc:
            log.warning("  Could not fetch subtask %s: %s", st_key, exc)

    return children


def get_previous_status_before_sfd(issue_key: str) -> Optional[str]:
    """
    Walks the full changelog of an issue and returns the 'fromString' status
    of the MOST RECENT transition into 'Selected for Development'.

    Returns None if no such transition exists in the history.
    """
    all_entries = []
    start_at = 0
    while True:
        data = jira_get(
            f"/rest/api/3/issue/{issue_key}/changelog"
            f"?startAt={start_at}&maxResults=100"
        )
        batch = data.get("values", [])
        all_entries.extend(batch)
        total = data.get("total", 0)
        start_at += len(batch)
        if start_at >= total or not batch:
            break

    # Scan forward; keep overwriting so we end up with the MOST RECENT match
    last_from = None
    for entry in all_entries:
        for item in entry.get("items", []):
            if (item.get("field") == "status"
                    and item.get("toString", "").lower() == "selected for development"):
                last_from = item.get("fromString", "")

    return last_from


# ═══════════════════════════════════════════════════════════════════════════════
#  Slack helpers
# ═══════════════════════════════════════════════════════════════════════════════

def slack_post(endpoint: str, data: dict) -> dict:
    """POST to Slack API and raise on error."""
    url = f"https://slack.com/api/{endpoint}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack {endpoint} error: {result.get('error', result)}")
    return result


def resolve_slack_id(jira_display_name: str) -> Optional[str]:
    """Return the Slack user ID for a Jira display name using substring matching.
    Returns None if no entry in JIRA_SLACK_USER_MAP matches."""
    name_lower = jira_display_name.lower()
    for key, uid in JIRA_SLACK_USER_MAP.items():
        if key.lower() in name_lower or name_lower in key.lower():
            return uid
    return None


def post_to_channel(channel: str, text: str) -> None:
    slack_post("chat.postMessage", {"channel": channel, "text": text})


# ═══════════════════════════════════════════════════════════════════════════════
#  Quality rule logic
# ═══════════════════════════════════════════════════════════════════════════════

def secs_to_human(seconds: int) -> str:
    """Convert seconds to a readable estimate string, e.g. 3600 → '1h'."""
    if not seconds:
        return "0"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) or "0"


def check_initiatives() -> list:
    """
    Rule: Initiative's own originalEstimate must equal the sum of
    originalEstimates of all non-Cancelled children.

    Returns a list of violation dicts:
    {
      "key": "INI-42",
      "summary": "...",
      "initiative_estimate": 7200,       # seconds (None = not set)
      "children_sum": 3600,              # sum of non-cancelled children
      "children": [{"key":..,"summary":..,"estimate":..,"cancelled":bool}]
    }
    """
    log.info("=== Jira Quality Check started at %s ===", datetime.now().isoformat(timespec="seconds"))

    jql = 'project = INI AND issuetype = Initiative AND status = "Selected for Development"'
    log.info("Searching: %s", jql)
    initiatives = jira_search(
        jql, fields="key,summary,subtasks,timeoriginalestimate,reporter"
    )
    log.info("Found %d Initiative(s) in 'Selected for Development'", len(initiatives))

    violations = []

    for issue in initiatives:
        key              = issue["key"]
        fields           = issue["fields"]
        summary          = fields["summary"]
        ini_estimate     = fields.get("timeoriginalestimate") or 0  # seconds
        reporter_name    = (fields.get("reporter") or {}).get("displayName", "")
        log.info("\nChecking %s: %s  (own estimate: %s, reporter: %s)",
                 key, summary, secs_to_human(ini_estimate), reporter_name)

        # ── Filter: only check tickets that freshly entered SFD ──────────────
        prev_status = get_previous_status_before_sfd(key)
        if prev_status is None:
            log.warning("  SKIP %s — no SFD transition found in changelog", key)
            continue
        if prev_status.lower() not in FRESH_PREVIOUS_STATUSES:
            log.info("  SKIP %s — came from %r (returned from dev/QA, not new)", key, prev_status)
            continue
        log.info("  Previous status: %r — qualifies as freshly entered SFD ✅", prev_status)
        # ─────────────────────────────────────────────────────────────────────

        subtask_keys = {s["key"] for s in fields.get("subtasks", [])}
        children     = get_initiative_children(key, subtask_keys)
        log.info("  Total children fetched: %d", len(children))

        children_detail = []
        children_sum    = 0

        for child in children:
            cf         = child.get("fields", {})
            c_key      = child["key"]
            c_summary  = cf.get("summary", "")
            c_status   = (cf.get("status", {}).get("name") or "").strip().lower()
            c_type     = (cf.get("issuetype", {}).get("name") or "").strip().lower()
            c_est      = cf.get("timeoriginalestimate") or 0  # seconds
            cancelled  = c_status == "cancelled"
            testing    = c_type == "testing"

            # Skip Testing and Cancelled — don't count, don't show in notification
            if testing:
                log.info("    SKIP (testing)   %s  estimate=%s", c_key, secs_to_human(c_est))
                continue
            if cancelled:
                log.info("    SKIP (cancelled) %s  estimate=%s", c_key, secs_to_human(c_est))
                continue

            children_detail.append({
                "key":      c_key,
                "summary":  c_summary,
                "estimate": c_est,
            })
            children_sum += c_est
            log.info("    COUNT %s  estimate=%s  running_sum=%s",
                     c_key, secs_to_human(c_est), secs_to_human(children_sum))

        log.info("  Initiative estimate: %s | Children sum: %s | Match: %s",
                 secs_to_human(ini_estimate), secs_to_human(children_sum),
                 "✅" if ini_estimate == children_sum else "❌")

        if ini_estimate != children_sum:
            violations.append({
                "key":                 key,
                "summary":             summary,
                "initiative_estimate": ini_estimate,
                "children_sum":        children_sum,
                "children":            children_detail,
                "reporter_name":       reporter_name,
            })

    log.info("\n=== %d violation(s) found ===", len(violations))
    return violations


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    violations = check_initiatives()

    if not violations:
        log.info("No violations — nothing to report. Exiting.")
        return

    for v in violations:
        emoji    = random.choice(EMOJIS)
        url      = f"{JIRA_URL}/browse/{v['key']}"

        slack_id = resolve_slack_id(v["reporter_name"])
        mention  = f" <@{slack_id}>" if slack_id else ""
        log.info("  Reporter: %r → Slack ID: %s", v["reporter_name"], slack_id or "not found")

        message = (
            f"{emoji} <{url}|{v['summary']}>{mention}"
            f" - it seems child elements do not match parent estimation."
            f" Can you check, please?"
        )
        try:
            post_to_channel(SLACK_CHANNEL, message)
            log.info("Posted to #%s for %s", SLACK_CHANNEL, v["key"])
        except Exception as exc:
            log.error("Failed to post for %s: %s", v["key"], exc)

    log.info("Done.\n")


if __name__ == "__main__":
    main()
