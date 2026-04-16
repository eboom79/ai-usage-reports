#!/usr/bin/env python3
"""
Slack bot – AI usage report manager.

Commands:
  "generate report(s)" – generates all TL reports in the background,
                          posts a channel update as each one finishes.
  "get my report" /
  "get report" /
  "report"             – DMs the TL their cached report with the date
                          it was generated.

Usage:
    python slack_bot.py

Setup: see README / .env.example for required tokens and scopes.
"""

import asyncio
import argparse
import json
import logging
import os
import pathlib
import re
import threading
from datetime import datetime
from io import BytesIO
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI usage Slack bot")
    parser.add_argument(
        "--env-file",
        default=os.getenv("ENV_FILE", ".env"),
        help="Path to the dotenv file to load before starting the bot.",
    )
    return parser.parse_args()


ARGS = _parse_args()
load_dotenv(ARGS.env_file)

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
SERVE_ONLY = os.getenv("SERVE_ONLY", "false").lower() == "true"
TEAM_LEADERS_FILE = os.getenv("TEAM_LEADERS_FILE", "team_leaders.json")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "eyal.boumgarten@redis.com").lower()

if not MOCK_MODE and not SERVE_ONLY:
    from hex_screenshot import CHROME_DEBUG_PORT, _extract_cookies_async, _screenshot_one, _open_hex_login_async, HexLoginRequired, launch_chrome_to_login
    from playwright.async_api import async_playwright

# ── Report cache: email (lowercase) → {png: bytes, ts: datetime} ─────────────
_report_cache: dict[str, dict] = {}

# ── Persistent report storage ─────────────────────────────────────────────────
REPORTS_DIR = pathlib.Path(os.getenv("REPORTS_DIR", str(pathlib.Path.home() / ".hex_reports")))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _save_report_to_disk(email: str, name: str, png: bytes, ts: datetime) -> None:
    """Persist a report PNG + metadata so it survives bot restarts."""
    safe = email.replace("@", "_at_").replace(".", "_")
    (REPORTS_DIR / f"{safe}.png").write_bytes(png)
    (REPORTS_DIR / f"{safe}.json").write_text(json.dumps({
        "name": name, "email": email, "ts": ts.isoformat(),
    }))
    log.info("Saved report for %s to disk (%s)", name, REPORTS_DIR / f"{safe}.png")


def _load_cache_from_disk() -> None:
    """On startup, reload any previously generated reports into _report_cache."""
    for meta_path in REPORTS_DIR.glob("*.json"):
        try:
            meta     = json.loads(meta_path.read_text())
            png_path = meta_path.with_suffix(".png")
            if not png_path.exists():
                continue
            _report_cache[meta["email"].lower()] = {
                "png": png_path.read_bytes(),
                "ts":  datetime.fromisoformat(meta["ts"]),
            }
            log.info("Restored cached report for %s (generated %s)", meta["name"], meta["ts"])
        except Exception as exc:
            log.warning("Could not load cached report from %s: %s", meta_path, exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "")   # empty = any channel

GOOGLE_DRIVE_CREDENTIALS = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "")
GOOGLE_DRIVE_CREDENTIALS_JSON = os.getenv("GOOGLE_DRIVE_CREDENTIALS_JSON", "")
GOOGLE_DRIVE_FOLDER_ID   = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

# When True: bot only serves cached reports — generation commands are disabled.
# Set SERVE_ONLY=true in .env on the cloud VM.

app = App(token=SLACK_BOT_TOKEN)


# ── Google Drive upload ───────────────────────────────────────────────────────

def _drive_service():
    """Build and return an authenticated Google Drive service client."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    if GOOGLE_DRIVE_CREDENTIALS_JSON:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_DRIVE_CREDENTIALS_JSON),
            scopes=scopes,
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_DRIVE_CREDENTIALS,
            scopes=scopes,
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_to_drive(name: str, email: str, png_bytes: bytes, generated_at: datetime) -> None:
    """Upload a report PNG to Google Drive. Fails silently so it never breaks the main flow."""
    if (not GOOGLE_DRIVE_CREDENTIALS and not GOOGLE_DRIVE_CREDENTIALS_JSON) or not GOOGLE_DRIVE_FOLDER_ID:
        return
    try:
        from googleapiclient.http import MediaIoBaseUpload

        service  = _drive_service()
        date_str = generated_at.strftime("%Y-%m-%d_%H-%M")
        filename = f"{name.replace(' ', '_')}_{date_str}.png"
        file_metadata = {
            "name":    filename,
            "parents": [GOOGLE_DRIVE_FOLDER_ID],
            "properties": {
                "email":        email.lower(),
                "generated_at": generated_at.isoformat(),
                "person_name":  name,
            },
        }
        media = MediaIoBaseUpload(BytesIO(png_bytes), mimetype="image/png")
        service.files().create(
            body=file_metadata, media_body=media, fields="id",
            supportsAllDrives=True,
        ).execute()
        log.info("Uploaded report for %s to Google Drive (%s)", name, filename)
    except Exception as exc:
        log.error("Google Drive upload failed for %s: %s", name, exc)


def _load_cache_from_drive() -> None:
    """On startup, download the latest report per person from Drive into _report_cache."""
    if (not GOOGLE_DRIVE_CREDENTIALS and not GOOGLE_DRIVE_CREDENTIALS_JSON) or not GOOGLE_DRIVE_FOLDER_ID:
        return
    try:
        service = _drive_service()

        # List all PNG files in the folder that have our custom properties
        results = service.files().list(
            q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and mimeType='image/png' and trashed=false",
            fields="files(id, name, properties)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=200,
        ).execute()

        files = results.get("files", [])
        log.info("Found %d report file(s) in Google Drive", len(files))

        # Group by email, keep the latest generated_at per person
        latest: dict[str, dict] = {}
        for f in files:
            props = f.get("properties") or {}
            email = props.get("email", "").lower()
            ts_str = props.get("generated_at", "")
            if not email or not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if email not in latest or ts > latest[email]["ts"]:
                latest[email] = {"file_id": f["id"], "ts": ts, "name": props.get("person_name", email)}

        # Download and cache the latest report for each person
        for email, info in latest.items():
            try:
                png_bytes = service.files().get_media(
                    fileId=info["file_id"], supportsAllDrives=True
                ).execute()
                _report_cache[email] = {"png": png_bytes, "ts": info["ts"]}
                log.info("Restored report for %s from Google Drive (generated %s)",
                         info["name"], info["ts"].strftime("%Y-%m-%d %H:%M"))
            except Exception as exc:
                log.error("Failed to download Drive report for %s: %s", email, exc)

    except Exception as exc:
        log.error("Failed to load reports from Google Drive: %s", exc)


def _refresh_report_from_drive(email: str) -> None:
    """Refresh one person's latest report from Drive into _report_cache."""
    if (not GOOGLE_DRIVE_CREDENTIALS and not GOOGLE_DRIVE_CREDENTIALS_JSON) or not GOOGLE_DRIVE_FOLDER_ID:
        return
    try:
        service = _drive_service()
        results = service.files().list(
            q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and mimeType='image/png' and trashed=false",
            fields="files(id, name, properties)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=200,
        ).execute()

        latest_file = None
        latest_ts = None
        target_email = email.lower()
        for f in results.get("files", []):
            props = f.get("properties") or {}
            file_email = props.get("email", "").lower()
            ts_str = props.get("generated_at", "")
            if file_email != target_email or not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_file = f

        if not latest_file or latest_ts is None:
            return

        cached = _report_cache.get(target_email)
        if cached and cached["ts"] >= latest_ts:
            return

        png_bytes = service.files().get_media(
            fileId=latest_file["id"], supportsAllDrives=True
        ).execute()
        _report_cache[target_email] = {"png": png_bytes, "ts": latest_ts}
        log.info("Refreshed report for %s from Google Drive (generated %s)",
                 target_email, latest_ts.strftime("%Y-%m-%d %H:%M"))
    except Exception as exc:
        log.error("Failed to refresh Drive report for %s: %s", email, exc)

# ── Team leader helpers ───────────────────────────────────────────────────────

def _load_tree() -> list[dict]:
    """Load the full team-leader tree from JSON (returns root list)."""
    try:
        with open(TEAM_LEADERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _flatten_tree(nodes: list[dict]) -> list[dict]:
    """Return every node in the tree as a flat list (DFS order)."""
    result: list[dict] = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten_tree(node.get("reports", [])))
    return result


def _load_team_leaders() -> list[dict]:
    """Return all people in the tree as a flat list."""
    return _flatten_tree(_load_tree())


def _find_leader_by_query(query: str) -> Optional[dict]:
    """Find a person by full name, first name, or surname (case-insensitive)."""
    q = query.strip().lower()
    if not q:
        return None
    for leader in _load_team_leaders():
        parts   = leader.get("name", "").strip().lower().split()
        full    = " ".join(parts)
        first   = parts[0]  if parts else ""
        surname = parts[-1] if parts else ""
        if q in (full, first, surname):
            return leader
    return None


def _can_access_report(requester_email: str, target_email: str) -> bool:
    """True if requester == target, OR target is a descendant of requester in the tree."""
    r = requester_email.lower()
    t = target_email.lower()
    if r == t:
        return True
    tree = _load_tree()
    all_nodes = _flatten_tree(tree)
    requester_node = next((n for n in all_nodes if n.get("email", "").lower() == r), None)
    if not requester_node:
        return False
    descendants = _flatten_tree(requester_node.get("reports", []))
    return any(n.get("email", "").lower() == t for n in descendants)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_info(client, user_id: str) -> tuple[str, str]:
    """Return (full_name, email) for a Slack user ID."""
    resp = client.users_info(user=user_id)
    profile = resp["user"]["profile"]
    name  = profile.get("real_name") or resp["user"].get("real_name", user_id)
    email = profile.get("email", "")
    return name, email


def _reply(say, thread_ts: str, text: str) -> None:
    say(text=text, thread_ts=thread_ts)


def _send_dm(client, user_id: str, text: str, blocks: Optional[list] = None) -> None:
    """Open a DM channel with the user and post a message."""
    dm = client.conversations_open(users=[user_id])
    dm_channel = dm["channel"]["id"]
    client.chat_postMessage(channel=dm_channel, text=text, blocks=blocks)


# ── Generate ONE report (background thread) ───────────────────────────────────

def _generate_one_in_background(client, channel: str, thread_ts: str, leader: dict) -> None:
    """Generate and cache a single TL's report, posting one channel update."""

    async def _run() -> None:
        name  = leader["name"]
        email = leader.get("email", "").lower()
        url   = leader.get("hex_url", "")

        if not url:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"⚠️ No Hex URL configured for *{name}*."
            )
            return

        try:
            cookies = await _extract_cookies_async(CHROME_DEBUG_PORT)
        except Exception:
            log.warning("Chrome not reachable — launching and retrying in 4 s …")
            launch_chrome_to_login()
            await asyncio.sleep(4)
            try:
                cookies = await _extract_cookies_async(CHROME_DEBUG_PORT)
                log.info("Chrome started successfully, proceeding with report generation.")
            except Exception as exc:
                log.error("Chrome still not reachable after launch: %s", exc)
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="🔒 Chrome opened but isn't ready yet — please log in to Hex, then try again."
                )
                return

        async with async_playwright() as p:
            try:
                png = await _screenshot_one(p, url, cookies)
                ts  = datetime.now()
                _report_cache[email] = {"png": png, "ts": ts}
                threading.Thread(target=_upload_to_drive, args=(name, email, png, ts), daemon=True).start()
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f"✅ Report ready for *{name}*"
                )
                log.info("Cached report for %s (%d bytes)", name, len(png))
            except HexLoginRequired:
                log.warning("Hex session expired — login required")
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="🔒 Hex is not logged in. Please log in via the browser that just opened, then try again."
                )
                await _open_hex_login_async()
            except Exception as exc:
                log.error("Failed to generate report for %s: %s", name, exc)
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f"❌ Failed to generate report for *{name}*: `{exc}`"
                )

    asyncio.run(_run())


# ── Generate ALL reports (background thread) ──────────────────────────────────

def _generate_all_in_background(client, channel: str, thread_ts: str) -> None:
    """Generate reports for all team leaders sequentially, posting channel updates."""

    async def _run() -> None:
        leaders = _load_team_leaders()
        try:
            cookies = await _extract_cookies_async(CHROME_DEBUG_PORT)
        except Exception:
            log.warning("Chrome not reachable — launching and retrying in 4 s …")
            launch_chrome_to_login()
            await asyncio.sleep(4)
            try:
                cookies = await _extract_cookies_async(CHROME_DEBUG_PORT)
                log.info("Chrome started successfully, proceeding with batch generation.")
            except Exception as exc:
                log.error("Chrome still not reachable after launch: %s", exc)
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="🔒 Chrome opened but isn't ready yet — please log in to Hex, then try again."
                )
                return
        log.info("Starting batch generation for %d leaders", len(leaders))

        async with async_playwright() as p:
            for leader in leaders:
                name  = leader["name"]
                email = leader.get("email", "").lower()
                url   = leader.get("hex_url", "")

                if not url:
                    client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=f"⚠️ No Hex URL configured for *{name}* — skipping."
                    )
                    continue

                log.info("Generating report for %s", name)
                try:
                    png = await _screenshot_one(p, url, cookies)
                    ts  = datetime.now()
                    _report_cache[email] = {"png": png, "ts": ts}
                    threading.Thread(target=_upload_to_drive, args=(name, email, png, ts), daemon=True).start()
                    client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=f"✅ Report ready for *{name}*"
                    )
                    log.info("Cached report for %s (%d bytes)", name, len(png))
                except HexLoginRequired:
                    log.warning("Hex session expired — login required")
                    client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text="🔒 Hex is not logged in. Please log in via the browser that just opened, then run *generate reports* again."
                    )
                    await _open_hex_login_async()
                    return  # Abort — no point continuing without a valid session
                except Exception as exc:
                    log.error("Failed to generate report for %s: %s", name, exc)
                    client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=f"❌ Failed to generate report for *{name}*: `{exc}`"
                    )

        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="🎉 All reports generated! TLs can now type *report* to receive their report."
        )

    asyncio.run(_run())


# ── Send a TL's cached report via DM (background thread) ─────────────────────

def _stamp_timestamp(png_bytes: bytes, generated_at: str) -> bytes:
    """Add a 'Generated on …' footer to the bottom of the report image."""
    img = Image.open(BytesIO(png_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)

    text = f"Generated on {generated_at}"
    font_size = 22
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    bbox    = draw.textbbox((0, 0), text, font=font)
    tw, th  = bbox[2] - bbox[0], bbox[3] - bbox[1]
    padding = 10
    x = img.width - tw - padding * 2
    y = padding

    # Semi-transparent background pill
    draw.rectangle([x - padding, y - padding, x + tw + padding, y + th + padding],
                   fill=(255, 255, 255, 200))
    draw.text((x, y), text, font=font, fill=(80, 80, 80, 255))

    out = BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _send_report_in_background(client, say, thread_ts: str, user_id: str, name: str, email: str) -> None:
    """Upload the cached report PNG directly to the TL's Slack DM."""
    _refresh_report_from_drive(email)
    log.info("Report lookup for %s: slack_email=%s cache_keys=%s",
             name, email.lower(), list(_report_cache.keys()))
    cached = _report_cache.get(email.lower())

    if not cached:
        _reply(say, thread_ts,
               f"⚠️ Hi {name}, no report has been generated for you yet. "
               f"Ask someone to type *generate reports* first.")
        return

    png_bytes    = cached["png"]
    generated_at = cached["ts"].strftime("%A, %B %-d %Y at %-I:%M %p")
    stamped      = _stamp_timestamp(png_bytes, generated_at)

    try:
        dm = client.conversations_open(users=[user_id])
        dm_channel = dm["channel"]["id"]

        client.files_upload_v2(
            channel=dm_channel,
            file=BytesIO(stamped),
            filename=f"ai_usage_report_{name.replace(' ', '_')}.png",
            title=f"📊 AI Usage Report – {name}",
            initial_comment=f"📊 *AI Usage Report – {name}*\n_Generated on {generated_at}_",
        )
        log.info("Report uploaded to DM for %s (generated %s)", name, generated_at)
        _reply(say, thread_ts, f"✅ Report sent to you as a DM, {name}! _(Generated {generated_at})_")

    except Exception as exc:
        log.error("Failed to send report to %s: %s", name, exc)
        _reply(say, thread_ts,
               f"❌ Sorry {name}, I couldn't send your report: `{exc}`")


# ── Message handlers ──────────────────────────────────────────────────────────

@app.message(re.compile(r"generate\s+reports?\s*(.*)", re.IGNORECASE))
def handle_generate_all_reports(message, client, say, context) -> None:
    """Generate report(s). With a name → one TL; without → all TLs."""
    if message.get("bot_id"):
        return
    if SERVE_ONLY:
        thread_ts = message.get("thread_ts") or message["ts"]
        _reply(say, thread_ts,
               "⚠️ Report generation is only available from the local machine. "
               "Existing reports can still be retrieved with *get report*.")
        return
    log.info("generate reports received from channel=%s (configured=%s)", message.get("channel"), SLACK_CHANNEL_ID)
    if SLACK_CHANNEL_ID and message.get("channel") != SLACK_CHANNEL_ID:
        log.warning("Ignoring message — channel mismatch: got %s, expected %s", message.get("channel"), SLACK_CHANNEL_ID)
        return

    user_id   = message["user"]
    channel   = message["channel"]
    thread_ts = message.get("thread_ts") or message["ts"]

    try:
        requester, _ = _get_user_info(client, user_id)
    except Exception:
        requester = "there"

    # Check if a name was typed after "generate report(s)"
    name_query = (context.get("matches") or [""])[0].strip()

    if name_query:
        # ── Single TL ──
        leader = _find_leader_by_query(name_query)
        if not leader:
            _reply(say, thread_ts,
                   f"⚠️ I couldn't find a team leader matching *{name_query}*. "
                   f"Try using their full name or surname.")
            return
        _reply(say, thread_ts,
               f"⏳ Generating report for *{leader['name']}*…")
        threading.Thread(
            target=_generate_one_in_background,
            args=(client, channel, thread_ts, leader),
            daemon=True,
        ).start()
    else:
        # ── All TLs ──
        _reply(say, thread_ts,
               f"⏳ Got it, {requester}! Generating reports for all team leaders now — I'll post an update here as each one finishes.")
        threading.Thread(
            target=_generate_all_in_background,
            args=(client, channel, thread_ts),
            daemon=True,
        ).start()


@app.message(re.compile(r"get\s+report\s+(\S.*\S|\S+)", re.IGNORECASE))
def handle_get_report_for(message, client, say, context) -> None:
    """Manager command: 'get report <name>' — DMs the requester the named TL's cached report."""
    if message.get("bot_id"):
        return
    # Skip "generate report <name>" — handled by handle_generate_all_reports
    if re.search(r"generate", message.get("text", ""), re.IGNORECASE):
        return
    if SLACK_CHANNEL_ID and message.get("channel") != SLACK_CHANNEL_ID:
        return

    user_id   = message["user"]
    thread_ts = message.get("thread_ts") or message["ts"]
    name_query = (context.get("matches") or [""])[0].strip()

    try:
        requester_name, requester_email = _get_user_info(client, user_id)
    except Exception as exc:
        log.error("Could not fetch profile for user %s: %s", user_id, exc)
        _reply(say, thread_ts, "❌ I couldn't look up your Slack profile.")
        return

    target = _find_leader_by_query(name_query)
    if not target:
        _reply(say, thread_ts,
               f"⚠️ I couldn't find a team leader matching *{name_query}*.")
        return

    target_email = target.get("email", "")
    if not _can_access_report(requester_email, target_email):
        _reply(say, thread_ts,
               f"🚫 You don't have permission to view *{target['name']}*'s report.")
        return

    _reply(say, thread_ts,
           f"⏳ Fetching *{target['name']}*'s report for you, {requester_name}…")
    threading.Thread(
        target=_send_report_in_background,
        args=(client, say, thread_ts, user_id, target["name"], target_email),
        daemon=True,
    ).start()


@app.message(re.compile(r"(?:get\s+(?:my\s+)?report|\breport\b)", re.IGNORECASE))
def handle_get_report(message, client, say) -> None:
    """Send the TL their cached report as a DM."""
    if message.get("bot_id"):
        return
    text = message.get("text", "")
    # Skip "generate report …" — handled by handle_generate_all_reports
    if re.search(r"generate", text, re.IGNORECASE):
        return
    # Skip "get report <name>" — handled by handle_get_report_for
    if re.search(r"get\s+report\s+\S", text, re.IGNORECASE):
        return
    if SLACK_CHANNEL_ID and message.get("channel") != SLACK_CHANNEL_ID:
        return

    user_id   = message["user"]
    thread_ts = message.get("thread_ts") or message["ts"]

    try:
        name, email = _get_user_info(client, user_id)
    except Exception as exc:
        log.error("Could not fetch profile for user %s: %s", user_id, exc)
        _reply(say, thread_ts, "❌ I couldn't look up your Slack profile. Please contact your admin.")
        return

    if not email:
        _reply(say, thread_ts,
               f"⚠️ Hi {name}, I can't find an email on your Slack profile.")
        return

    _reply(say, thread_ts, f"⏳ Fetching your report, {name}…")

    threading.Thread(
        target=_send_report_in_background,
        args=(client, say, thread_ts, user_id, name, email),
        daemon=True,
    ).start()


@app.event("message")
def handle_all_messages(body, logger):
    event = body.get("event", {})
    logger.info("RAW message event: channel=%s subtype=%s text=%r",
                event.get("channel"), event.get("subtype"), event.get("text", "")[:80])


# ── Single-instance guard (PID file) ─────────────────────────────────────────

PID_FILE = pathlib.Path("/tmp/slack_bot.pid")

def _acquire_pid_file() -> None:
    """Refuse to start if another instance is already running."""
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text().strip())
            # Signal 0 checks if the process is alive without killing it
            os.kill(existing_pid, 0)
            log.error(
                "Another instance is already running (PID %d). "
                "Kill it first or delete %s.", existing_pid, PID_FILE
            )
            raise SystemExit(1)
        except ProcessLookupError:
            log.warning("Stale PID file found — previous instance is gone. Overwriting.")
    PID_FILE.write_text(str(os.getpid()))
    log.info("PID file written: %s (PID %d)", PID_FILE, os.getpid())

def _release_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _acquire_pid_file()
    try:
        log.info("Starting AI usage report Slack bot (Socket Mode) …")
        _load_cache_from_drive()
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        handler.start()
    finally:
        _release_pid_file()
