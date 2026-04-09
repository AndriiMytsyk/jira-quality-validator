#!/usr/bin/env python3
"""
Jira Quality Validator — Claude Agent
======================================
Claude Opus 4.6 uses tool use to autonomously:
  1. Fetch Initiatives in "Selected for Development" from the INI project.
  2. Filter to only freshly entered tickets (previous status must be
     Backlog / Ready for Refinement / Discovery).
  3. Compare each Initiative's originalEstimate to the sum of its
     non-Cancelled, non-Testing children's estimates.
  4. Post each violation as a separate Slack message to #b2b-dev-estimates,
     @mentioning the ticket reporter when known.

Designed to run in GitHub Actions (see .github/workflows/jira-quality-check.yml).
All credentials are read from environment variables; nothing is hard-coded.
"""

import json
import base64
import sys
import os
import urllib.request
import urllib.error
import logging
from anthropic import Anthropic

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Credentials (from environment variables) ───────────────────────────────────
JIRA_URL    = os.environ.get("JIRA_URL",   "https://gmntc.atlassian.net")
JIRA_EMAIL  = os.environ.get("JIRA_EMAIL", "andrii.mytsyk@gamingtec.com")
JIRA_TOKEN  = os.environ["JIRA_TOKEN"]
SLACK_TOKEN = os.environ["SLACK_TOKEN"]
# ANTHROPIC_API_KEY is picked up automatically by the Anthropic SDK

# ── App config ─────────────────────────────────────────────────────────────────
SLACK_CHANNEL = "U04P18W82AF"  # TODO: revert to "b2b-dev-estimates" after testing
EMOJIS        = ["🫪", "🐝", "🫈"]

# Jira display name (substring) → Slack user ID
JIRA_SLACK_USER_MAP = {
    "Alexandra Yavorska": "U04T8LRLQ2Z",
    "Jahor":              "U087A01BJJU",
    "Tati":               "U06MEGXR82E",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Low-level HTTP helpers (no external libraries beyond anthropic)
# ══════════════════════════════════════════════════════════════════════════════

def _jira_auth() -> str:
    return "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()


def jira_post(path: str, body: dict) -> dict:
    url = JIRA_URL + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": _jira_auth(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        log.error("Jira POST %s HTTP %s: %s", path, e.code, body_err)
        raise


def jira_get(path: str) -> dict:
    url = JIRA_URL + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": _jira_auth(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        log.error("Jira GET %s HTTP %s: %s", path, e.code, body_err)
        raise


def slack_post_msg(channel: str, text: str) -> None:
    url = "https://slack.com/api/chat.postMessage"
    req = urllib.request.Request(
        url,
        data=json.dumps({"channel": channel, "text": text}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack error: {result.get('error', result)}")


# ══════════════════════════════════════════════════════════════════════════════
#  Tool implementations (called when Claude invokes a tool)
# ══════════════════════════════════════════════════════════════════════════════

def tool_search_jira(jql: str, fields: list, max_results: int = 200) -> dict:
    """POST /rest/api/3/search/jql with cursor pagination."""
    issues = []
    next_page_token = None

    while True:
        body: dict = {
            "jql": jql,
            "fields": fields,
            "maxResults": min(max_results - len(issues), 100),
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = jira_post("/rest/api/3/search/jql", body)
        batch = data.get("issues", [])
        issues.extend(batch)

        if data.get("isLast", True) or not batch or len(issues) >= max_results:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return {"issues": issues[:max_results], "total": len(issues[:max_results])}


def tool_get_issue_changelog(issue_key: str) -> dict:
    """Fetch the full changelog for a single issue."""
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
    return {"issue_key": issue_key, "changelog": all_entries}


def tool_post_slack_message(channel: str, text: str) -> dict:
    slack_post_msg(channel, text)
    log.info("Slack message posted to #%s", channel)
    return {"ok": True, "channel": channel}


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def execute_tool(name: str, inp: dict) -> str:
    log.info("  → tool:%s  input:%s", name, json.dumps(inp)[:300])
    try:
        if name == "search_jira":
            result = tool_search_jira(**inp)
        elif name == "get_issue_changelog":
            result = tool_get_issue_changelog(**inp)
        elif name == "post_slack_message":
            result = tool_post_slack_message(**inp)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        log.error("Tool %s raised: %s", name, exc)
        result = {"error": str(exc)}

    out = json.dumps(result)
    log.info("  ← tool result: %s", out[:300])
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Tool schemas for the Anthropic API
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "search_jira",
        "description": (
            "Search Jira issues via JQL. Uses the Jira Cloud POST /rest/api/3/search/jql endpoint "
            "(GET /search is deprecated/gone on Jira Cloud — do NOT use it). "
            "Returns a list of issues with the requested fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {
                    "type": "string",
                    "description": "JQL query string, e.g. 'project = INI AND issuetype = Initiative AND status = \"Selected for Development\"'"
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Field names to include in each issue, e.g. "
                        "[\"key\", \"summary\", \"timeoriginalestimate\", \"status\", "
                        "\"issuetype\", \"reporter\", \"subtasks\"]"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum issues to return (default 200).",
                    "default": 200,
                },
            },
            "required": ["jql", "fields"],
        },
    },
    {
        "name": "get_issue_changelog",
        "description": (
            "Fetch the complete status-change history for a single Jira issue. "
            "Each entry has 'items' with fields: field, fromString, toString. "
            "Use this to find what status an issue came from before 'Selected for Development'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Jira issue key, e.g. 'INI-42'",
                }
            },
            "required": ["issue_key"],
        },
    },
    {
        "name": "post_slack_message",
        "description": (
            "Post a single message to a Slack channel. "
            "Supports Slack mrkdwn: use <URL|text> for clickable links, <@USER_ID> for mentions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Slack channel name (without #) or channel ID",
                },
                "text": {
                    "type": "string",
                    "description": "Message text in Slack mrkdwn format",
                },
            },
            "required": ["channel", "text"],
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  System prompt
# ══════════════════════════════════════════════════════════════════════════════

_map_lines = "\n".join(
    f"  • '{name}' (substring match) → <@{uid}>"
    for name, uid in JIRA_SLACK_USER_MAP.items()
)

SYSTEM_PROMPT = f"""You are an automated Jira quality validator. Your task is to check estimate consistency \
for Initiative tickets and report violations to Slack.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 QUALITY RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For every Initiative in the INI project with status "Selected for Development":

STEP 1 — Freshness filter (check the changelog)
  • Fetch the issue's full changelog with get_issue_changelog.
  • Find the MOST RECENT entry where an item has field="status" and toString="Selected for Development".
  • Look at that entry's fromString (the status it came FROM).
  • PROCEED only if fromString is (case-insensitive) one of:
      "backlog" | "ready for refinement" | "discovery"
  • SKIP if fromString is anything else (e.g. "In Progress", "Testing in Progress", "on Hold") —
    the ticket returned from dev/QA and should not be rechecked.
  • SKIP if no "Selected for Development" transition exists in the changelog at all.

STEP 2 — Estimate comparison
  • Note the Initiative's timeoriginalestimate (seconds; null = 0).
  • Search for children: JQL  parent = {{KEY}}
    fields: key, summary, status, issuetype, timeoriginalestimate
  • Exclude any child where status.name = "Cancelled" (case-insensitive).
  • Exclude any child where issuetype.name = "Testing" (case-insensitive).
  • Sum timeoriginalestimate of all remaining children (null = 0).
  • VIOLATION if: Initiative's estimate ≠ sum of children's estimates.

STEP 3 — Report each violation
  • Post ONE Slack message per violation to channel: {SLACK_CHANNEL}
  • Pick a random emoji from: {EMOJIS}
  • Reporter → Slack mention mapping (case-insensitive substring match on reporter's displayName):
{_map_lines}
  • If the reporter is NOT in the map, omit the mention entirely.
  • Message format (Slack mrkdwn):
    {{emoji}} <{JIRA_URL}/browse/{{KEY}}|{{Initiative summary}}>{{mention}} - it seems child elements do not match parent estimation. Can you check, please?
  • Example with mention:
    🐝 <{JIRA_URL}/browse/INI-42|My Initiative> <@U04T8LRLQ2Z> - it seems child elements do not match parent estimation. Can you check, please?
  • Example without mention:
    🫪 <{JIRA_URL}/browse/INI-7|Another Initiative> - it seems child elements do not match parent estimation. Can you check, please?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 IMPORTANT NOTES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Process EVERY initiative — do not skip without checking the changelog first.
• Use random.choice logic mentally when picking emojis (vary them across messages).
• After all checks, output a plain-text summary: how many initiatives found, how many skipped, how many violations reported.
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Agentic loop
# ══════════════════════════════════════════════════════════════════════════════

def run_agent() -> None:
    client = Anthropic()

    messages = [
        {
            "role": "user",
            "content": (
                "Run the Jira quality check now. "
                "Check all Initiative tickets in 'Selected for Development' in the INI project "
                "and report any estimation mismatches to Slack."
            ),
        }
    ]

    log.info("=== Jira Quality Check Agent starting ===")
    iteration = 0
    max_iterations = 60  # safety cap — a typical run uses ~10–20 turns

    while iteration < max_iterations:
        iteration += 1
        log.info("--- Turn %d ---", iteration)

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        log.info("Stop reason: %s  |  usage: input=%d output=%d",
                 response.stop_reason,
                 response.usage.input_tokens,
                 response.usage.output_tokens)

        # Append full assistant response (including any thinking blocks — required by the API)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    log.info("=== Agent summary ===\n%s", block.text)
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result_content = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
        else:
            log.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

    if iteration >= max_iterations:
        log.error("Safety cap reached (%d iterations) — check for agent loop", max_iterations)

    log.info("=== Agent finished ===")


if __name__ == "__main__":
    run_agent()
