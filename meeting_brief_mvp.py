#!/usr/bin/env python3
"""
meeting_brief_mvp.py

Local MVP: auto-send pre-meeting briefs 15 mins before meetings.

Free-tier stack:
- Google Calendar API (free)
- Gmail API (free)
- DuckDuckGo HTML search scraping via requests + BeautifulSoup (free)
- Ollama local LLM (free)
- SQLite (free, local)

No paid APIs. No LinkedIn scraping.
"""

import os
import re
import json
import time
import base64
import sqlite3
import logging
import datetime as dt
from email.mime.text import MIMEText
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# -----------------------------
# Config
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Change these as needed
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "UTC")
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "15"))  # send brief for meetings starting in next 15 min
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "20"))  # detect upcoming meetings window
EMAIL_THREADS_LIMIT = int(os.getenv("EMAIL_THREADS_LIMIT", "5"))
DB_PATH = os.getenv("DB_PATH", "meeting_briefs.db")
INTERNAL_DOMAINS = set(
    d.strip().lower() for d in os.getenv("INTERNAL_DOMAINS", "").split(",") if d.strip()
)
# Example: INTERNAL_DOMAINS="yourcompany.com,subsidiary.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# -----------------------------
# Google Auth
# -----------------------------
def get_google_services():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Put your downloaded OAuth desktop app file as credentials.json
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    calendar_service = build("calendar", "v3", credentials=creds)
    gmail_service = build("gmail", "v1", credentials=creds)
    return calendar_service, gmail_service


# -----------------------------
# SQLite
# -----------------------------
def init_db(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_event_id TEXT NOT NULL,
            attendee_email TEXT NOT NULL,
            attendee_name TEXT,
            company TEXT,
            meeting_start TEXT NOT NULL,
            brief_subject TEXT NOT NULL,
            brief_body TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(meeting_event_id, attendee_email)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attendee_email TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def already_sent(conn: sqlite3.Connection, event_id: str, attendee_email: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM briefs WHERE meeting_event_id=? AND attendee_email=? LIMIT 1",
        (event_id, attendee_email),
    )
    return cur.fetchone() is not None


def save_brief(
    conn: sqlite3.Connection,
    event_id: str,
    attendee_email: str,
    attendee_name: str,
    company: str,
    meeting_start: str,
    subject: str,
    body: str,
):
    conn.execute(
        """
        INSERT OR IGNORE INTO briefs
        (meeting_event_id, attendee_email, attendee_name, company, meeting_start, brief_subject, brief_body, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            attendee_email,
            attendee_name,
            company,
            meeting_start,
            subject,
            body,
            dt.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()


def get_past_briefs_for_attendee(conn: sqlite3.Connection, attendee_email: str, limit=5) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT meeting_start, brief_subject, brief_body, sent_at
        FROM briefs
        WHERE attendee_email=?
        ORDER BY sent_at DESC
        LIMIT ?
        """,
        (attendee_email, limit),
    )
    rows = cur.fetchall()
    return [
        {"meeting_start": r[0], "subject": r[1], "body": r[2], "sent_at": r[3]}
        for r in rows
    ]


# -----------------------------
# Calendar
# -----------------------------
def get_user_primary_email(gmail_service) -> str:
    profile = gmail_service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "").lower()


def is_external(email: str, my_email: str) -> bool:
    email = (email or "").lower()
    if not email or email == my_email:
        return False
    domain = email.split("@")[-1]
    if INTERNAL_DOMAINS:
        return domain not in INTERNAL_DOMAINS
    # fallback: if no INTERNAL_DOMAINS set, treat everything not my exact domain as external
    my_domain = my_email.split("@")[-1] if "@" in my_email else ""
    return domain != my_domain


def parse_attendee(att: Dict[str, Any]) -> Dict[str, str]:
    email = att.get("email", "").strip().lower()
    display_name = att.get("displayName", "").strip()
    if not display_name and email:
        display_name = email.split("@")[0].replace(".", " ").title()
    company = email.split("@")[-1] if "@" in email else ""
    return {"email": email, "name": display_name, "company": company}


def get_upcoming_events(calendar_service) -> List[Dict[str, Any]]:
    now = dt.datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + dt.timedelta(minutes=LOOKAHEAD_MINUTES)).isoformat() + "Z"

    events_result = (
        calendar_service.events()
        .list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        )
        .execute()
    )
    return events_result.get("items", [])


def event_starts_in_target_window(event: Dict[str, Any], window_minutes=WINDOW_MINUTES) -> bool:
    start = event.get("start", {}).get("dateTime")
    if not start:
        return False  # skip all-day events
    start_dt = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    minutes = (start_dt - now).total_seconds() / 60
    # send if meeting starts between 10 and 16 minutes from now (buffer around exactly 15)
    return (window_minutes - 5) <= minutes <= (window_minutes + 1)


# -----------------------------
# Gmail history
# -----------------------------
def extract_plain_text_from_payload(payload: Dict[str, Any]) -> str:
    if not payload:
        return ""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    if mime_type == "text/plain" and data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    parts = payload.get("parts", [])
    for part in parts:
        text = extract_plain_text_from_payload(part)
        if text:
            return text
    return ""


def get_recent_email_threads_with_attendee(gmail_service, attendee_email: str, limit=EMAIL_THREADS_LIMIT) -> List[Dict[str, str]]:
    # Query messages exchanged with attendee in any direction
    query = f"(from:{attendee_email} OR to:{attendee_email}) newer_than:2y"
    res = gmail_service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    messages = res.get("messages", [])
    threads = []

    seen_thread_ids = set()
    for msg in messages:
        msg_detail = gmail_service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        thread_id = msg_detail.get("threadId")
        if not thread_id or thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread_id)

        thread = gmail_service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = thread.get("messages", [])
        if not msgs:
            continue

        # collect last 1-2 snippets from thread
        snippets = []
        subject = ""
        for m in msgs[-2:]:
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", subject)
            text = extract_plain_text_from_payload(m.get("payload", {}))
            if not text:
                text = m.get("snippet", "")
            snippets.append(text[:700])

        threads.append({
            "thread_id": thread_id,
            "subject": subject,
            "snippets": "\n---\n".join(snippets),
        })

        if len(threads) >= limit:
            break

    return threads


def send_email(gmail_service, to_email: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# -----------------------------
# Free web search (DuckDuckGo HTML)
# -----------------------------
def duckduckgo_search_news(query: str, max_results=5) -> List[Dict[str, str]]:
    """
    Free method: scrape DuckDuckGo HTML results.
    No paid API. No LinkedIn scraping.
    """
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.post(url, data={"q": query}, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for r in soup.select(".result"):
        a = r.select_one(".result__a")
        snippet = r.select_one(".result__snippet")
        if not a:
            continue
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        snip = snippet.get_text(" ", strip=True) if snippet else ""
        if "linkedin.com" in href.lower() or "linkedin.com" in snip.lower():
            continue
        results.append({"title": title, "url": href, "snippet": snip})
        if len(results) >= max_results:
            break
    return results


# -----------------------------
# LLM via Ollama (free local)
# -----------------------------
def generate_brief_with_ollama(payload: Dict[str, Any]) -> str:
    prompt = f"""
You are an executive meeting prep assistant.

Given the JSON context below, produce a concise pre-meeting brief with sections:

1) Who they are
2) What was last discussed
3) Suggested talking points (2-3 bullets)
4) Useful recent company/news context

Rules:
- Be factual and grounded only in provided context.
- If data is missing, say "Not enough info".
- Keep it under 250 words.
- Do not mention LinkedIn.
- Prioritize actionable points.

Context JSON:
{json.dumps(payload, indent=2)}
"""

    r = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        },
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("response", "").strip()


# -----------------------------
# Main flow
# -----------------------------
def pick_external_attendee(event: Dict[str, Any], my_email: str) -> Optional[Dict[str, str]]:
    attendees = event.get("attendees", []) or []
    externals = []
    for att in attendees:
        parsed = parse_attendee(att)
        if parsed["email"] and is_external(parsed["email"], my_email):
            externals.append(parsed)
    # MVP: pick first external attendee
    return externals[0] if externals else None


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def build_brief(
    event: Dict[str, Any],
    attendee: Dict[str, str],
    email_threads: List[Dict[str, str]],
    news_results: List[Dict[str, str]],
    past_briefs: List[Dict[str, Any]],
) -> str:
    event_start = event.get("start", {}).get("dateTime", "")
    event_title = event.get("summary", "(No title)")

    context = {
        "meeting": {
            "title": event_title,
            "start": event_start,
            "description": clean_text(event.get("description", ""))[:1000],
        },
        "attendee": attendee,
        "recent_email_threads": email_threads,
        "news_results": news_results,
        "past_briefs_with_same_attendee": past_briefs,
    }
    return generate_brief_with_ollama(context)


def run_once():
    calendar_service, gmail_service = get_google_services()
    my_email = get_user_primary_email(gmail_service)

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)

        events = get_upcoming_events(calendar_service)
        logging.info("Found %d upcoming events", len(events))

        for event in events:
            if not event_starts_in_target_window(event):
                continue

            event_id = event.get("id", "")
            start = event.get("start", {}).get("dateTime", "")
            title = event.get("summary", "(No title)")
            attendee = pick_external_attendee(event, my_email)

            if not attendee:
                logging.info("Skipping '%s' (no external attendee)", title)
                continue

            if already_sent(conn, event_id, attendee["email"]):
                logging.info("Brief already sent for event=%s attendee=%s", event_id, attendee["email"])
                continue

            logging.info("Preparing brief for '%s' with %s", title, attendee["email"])

            threads = get_recent_email_threads_with_attendee(gmail_service, attendee["email"], EMAIL_THREADS_LIMIT)
            news_query = f"{attendee['company']} recent news"
            news = duckduckgo_search_news(news_query, max_results=5)
            past = get_past_briefs_for_attendee(conn, attendee["email"], limit=3)

            brief = build_brief(event, attendee, threads, news, past)

            subject = f"Pre-Meeting Brief ({attendee['name']}) - {title}"
            body = f"""Meeting starts at: {start}
Attendee: {attendee['name']} <{attendee['email']}>
Company: {attendee['company']}

{brief}

---
Auto-generated by local Meeting Brief MVP
"""
            send_email(gmail_service, my_email, subject, body)
            save_brief(
                conn,
                event_id=event_id,
                attendee_email=attendee["email"],
                attendee_name=attendee["name"],
                company=attendee["company"],
                meeting_start=start,
                subject=subject,
                body=body,
            )
            logging.info("Brief sent to %s for event '%s'", my_email, title)


if __name__ == "__main__":
    # Simple daemon-like loop: check every 60 seconds
    # You can also run via cron and call run_once() only once.
    while True:
        try:
            run_once()
        except Exception as e:
            logging.exception("Error in run loop: %s", e)
        time.sleep(60)