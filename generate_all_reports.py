#!/usr/bin/env python3
"""
Generate screenshots for all team leaders and upload them to Google Drive.

Usage:
    python3 generate_all_reports.py
"""
import json
import asyncio
import logging
import os
from datetime import datetime
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

from hex_screenshot import _extract_cookies_async, _screenshot_one, launch_chrome_to_login
from playwright.async_api import async_playwright

TEAM_LEADERS_FILE        = os.getenv("TEAM_LEADERS_FILE", "team_leaders.json")
GOOGLE_DRIVE_CREDENTIALS = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "")
GOOGLE_DRIVE_FOLDER_ID   = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
SLACK_BOT_TOKEN          = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID         = os.getenv("SLACK_CHANNEL_ID", "")


def _notify_slack(success: int, failed: int) -> None:
    """Post a summary message to the Slack channel."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        if failed == 0:
            text = f"🎉 New reports have been generated and are ready — all {success} team leader reports are up to date. Type *report* to receive yours."
        else:
            text = f"⚠️ New reports have been generated and are ready — {success} succeeded, {failed} failed. Type *report* to receive yours."
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text)
        log.info("Slack notification sent.")
    except Exception as exc:
        log.error("Failed to send Slack notification: %s", exc)


def _upload_to_drive(name: str, email: str, png_bytes: bytes, generated_at: datetime) -> None:
    """Delete existing reports for this person in Google Drive, then upload the new one."""
    if not GOOGLE_DRIVE_CREDENTIALS or not GOOGLE_DRIVE_FOLDER_ID:
        log.warning("Google Drive not configured — skipping upload for %s", name)
        return
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_DRIVE_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        date_str = generated_at.strftime("%Y-%m-%d_%H-%M")
        filename = f"{name.replace(' ', '_')}_{date_str}.png"
        media = MediaIoBaseUpload(BytesIO(png_bytes), mimetype="image/png")

        # Find any existing report for this person
        name_prefix = name.replace(' ', '_') + "_"
        results = service.files().list(
            q=(
                f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents"
                f" and mimeType='image/png'"
                f" and trashed=false"
                f" and name contains '{name_prefix}'"
            ),
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        existing = results.get("files", [])

        if existing:
            # Update the first match in place (rename + replace content)
            file_id = existing[0]["id"]
            service.files().update(
                fileId=file_id,
                body={"name": filename},
                media_body=media,
                supportsAllDrives=True,
            ).execute()
            # Delete any extra duplicates
            for f in existing[1:]:
                try:
                    service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                except Exception:
                    pass
            log.info("[%s] ✅ Updated existing report in Google Drive (%s)", name, filename)
        else:
            # No existing file — create a new one
            file_metadata = {
                "name":    filename,
                "parents": [GOOGLE_DRIVE_FOLDER_ID],
            }
            service.files().create(
                body=file_metadata, media_body=media, fields="id",
                supportsAllDrives=True,
            ).execute()
            log.info("[%s] ✅ Uploaded to Google Drive (%s)", name, filename)
    except Exception as exc:
        log.error("[%s] ❌ Google Drive upload failed: %s", name, exc)


def _load_team_leaders() -> list[dict]:
    """Load and flatten the team leader tree from JSON."""
    def flatten(nodes):
        result = []
        for node in nodes:
            result.append(node)
            result.extend(flatten(node.get("reports", [])))
        return result

    with open(TEAM_LEADERS_FILE) as f:
        return flatten(json.load(f))


async def run_all():
    leaders = _load_team_leaders()
    log.info("Generating reports for %d team leaders...", len(leaders))

    try:
        cookies = await _extract_cookies_async(9222)
    except Exception:
        log.warning("Chrome not running — launching it now. Please log in to Hex if prompted, then the script will continue in 10 seconds...")
        launch_chrome_to_login()
        await asyncio.sleep(10)
        cookies = await _extract_cookies_async(9222)
    log.info("Cookies extracted: %d", len(cookies))

    success, failed = 0, 0
    async with async_playwright() as p:
        for leader in leaders:
            name  = leader["name"]
            email = leader.get("email", "").lower()
            url   = leader.get("hex_url", "")
            if not url:
                log.warning("[%s] No hex_url — skipping", name)
                continue

            log.info("[%s] Generating...", name)
            try:
                png = await _screenshot_one(p, url, cookies)
                ts  = datetime.now()
                _upload_to_drive(name, email, png, ts)
                success += 1
            except Exception as e:
                log.error("[%s] ❌ %s", name, e)
                failed += 1

    log.info("All done! %d succeeded, %d failed.", success, failed)
    _notify_slack(success, failed)


if __name__ == "__main__":
    asyncio.run(run_all())

