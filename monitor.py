#!/usr/bin/env python3
"""
Mr. Lodge watcher.

Checks a Mr. Lodge search page that is already filtered by max rent, and sends a
Telegram message when an offer appears that it has not seen before.

Local test (prints what it found, sends nothing, changes nothing):
    SEARCH_URL="https://www.mrlodge.com/short-term-rental-munich/1?rentMax=1150" python monitor.py --probe
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- settings ---------------------------------------------------------------

SEARCH_URLS = [
    u.strip()
    for u in os.environ.get(
        "SEARCH_URL",
        "https://www.mrlodge.com/short-term-rental-munich/1?rentMax=1150",
    ).split(",")
    if u.strip()
]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = Path("seen.json")
MAX_REMEMBERED = 2000
MAX_ALERTS_PER_RUN = 10
HEARTBEAT_DAYS = 7          # "I'm still alive" message every 7 days
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# The page is healthy if it contains this. Mr. Lodge prints it even when the
# result is "0 Offers found", so it tells "nothing matched" apart from "broken".
HEALTH_MARKER = re.compile(r"Offers?\s+found|Angebote\s+gefunden", re.I)

LISTING_HREF = re.compile(r"/rent/", re.I)
LISTING_ID = re.compile(r"(\d{3,7})\s*$")
PRICE = re.compile(r"([\d][\d.,]{2,})\s*€")
ROOMS = re.compile(r"([\d.,]+)\s*(?:room|Zimmer)", re.I)
AREA = re.compile(r"(\d+)\s*m²")

# ----------------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(2**attempt + random.random())
    raise RuntimeError(f"Could not load {url}: {last}")


def absolute(href):
    if href.startswith("http"):
        return href.split("?")[0]
    return "https://www.mrlodge.com" + href.split("?")[0]


def card_text(anchor):
    """Walk up from the link until we find a block that holds the price."""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if "€" in text and len(text) < 600:
            return text
    return anchor.get_text(" ", strip=True)


def parse(html):
    soup = BeautifulSoup(html, "lxml")
    found = {}

    for anchor in soup.find_all("a", href=LISTING_HREF):
        url = absolute(anchor.get("href", ""))
        match = LISTING_ID.search(url)
        if not match:
            continue
        listing_id = match.group(1)

        title = anchor.get_text(" ", strip=True)
        text = card_text(anchor)

        bits = []
        if (m := ROOMS.search(text)):
            bits.append(f"{m.group(1)} room")
        if (m := AREA.search(text)):
            bits.append(f"{m.group(1)} m²")
        price = (m.group(1) if (m := PRICE.search(text)) else "")

        existing = found.get(listing_id)
        # several links point at the same offer (photo, title); keep the best one
        if existing and (len(existing["title"]) >= len(title) or not title):
            continue
        found[listing_id] = {
            "id": listing_id,
            "title": title or f"Offer {listing_id}",
            "price": price,
            "details": " · ".join(bits),
            "url": url,
        }

    return list(found.values())


def load_state():
    if not STATE_FILE.exists():
        return {"initialised": False, "seen": [], "last_heartbeat": ""}
    try:
        state = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        log("state file unreadable, starting fresh")
        return {"initialised": False, "seen": [], "last_heartbeat": ""}
    state.setdefault("initialised", False)
    state.setdefault("seen", [])
    state.setdefault("last_heartbeat", "")
    return state


def save_state(state):
    state["seen"] = state["seen"][-MAX_REMEMBERED:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chat_ids():
    """
    Who gets the alerts. TELEGRAM_CHAT_ID may hold one id or several,
    separated by commas, so more than one person can follow along.
    Everyone listed must have pressed Start on the bot at least once.
    """
    return [x.strip() for x in TELEGRAM_CHAT_ID.split(",") if x.strip()]


def telegram(text):
    if not TELEGRAM_TOKEN or not chat_ids():
        log("no Telegram credentials set, message not sent")
        return
    problems = []
    for who in chat_ids():
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": who,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            problems.append(f"{who}: {r.text[:120]}")
    if problems:
        # one bad recipient must not stop the others from being told
        log("Telegram problems -> " + " | ".join(problems))


def message_for(listing):
    lines = ["\U0001f3e0 <b>New offer!</b>", escape(listing["title"])]
    facts = [x for x in (f"€{listing['price']}/month" if listing["price"] else "",
                         listing["details"]) if x]
    if facts:
        lines.append(escape(" · ".join(facts)))
    lines.append(listing["url"])
    return "\n".join(lines)


def heartbeat_due(state):
    if not state["last_heartbeat"]:
        return True
    try:
        last = datetime.fromisoformat(state["last_heartbeat"])
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last).days >= HEARTBEAT_DAYS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true",
                        help="show what was found and exit")
    args = parser.parse_args()

    listings = []
    for url in SEARCH_URLS:
        html = fetch(url)
        if not HEALTH_MARKER.search(html):
            # The page did not look like a Mr. Lodge result list at all.
            log(f"unexpected page content at {url}")
            return 1
        listings.extend(parse(html))
        time.sleep(1)

    log(f"{len(listings)} offer(s) match the filter right now")

    if args.probe:
        print(json.dumps(listings, indent=2, ensure_ascii=False))
        return 0

    state = load_state()
    seen = set(state["seen"])
    new = [x for x in listings if x["id"] not in seen]

    if not state["initialised"]:
        state["initialised"] = True
        state["seen"] = [x["id"] for x in listings]
        state["last_heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)
        telegram(
            "\u2705 Watcher started.\n"
            f"Right now there are {len(listings)} offers under your limit. "
            "From now on you will only hear from me when a new one appears."
        )
        return 0

    for listing in new[:MAX_ALERTS_PER_RUN]:
        telegram(message_for(listing))
        time.sleep(1)
    if len(new) > MAX_ALERTS_PER_RUN:
        telegram(f"…and {len(new) - MAX_ALERTS_PER_RUN} more new offers.")

    if new:
        log(f"sent {len(new)} alert(s)")
        state["seen"].extend(x["id"] for x in new)

    if heartbeat_due(state):
        telegram(
            "\U0001f440 Still watching. "
            f"{len(listings)} offers under your limit at the moment."
        )
        state["last_heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
