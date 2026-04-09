#!/usr/bin/env python3
"""
Part 1 – Generate and send an AI usage report for a single team leader.

Usage:
    python send_report.py --name "Alice Smith" --email alice@example.com

The script:
  1. Calls the Hex API to run the AI-usage project, passing the team
     leader's name as an input parameter so the report is filtered for
     their team.
  2. Polls until the run completes (or times out).
  3. Sends an email to the team leader with a link to their report.
"""

import argparse
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config (read from environment / .env) ────────────────────────────────────
HEX_API_TOKEN       = os.environ["HEX_API_TOKEN"]
HEX_PROJECT_ID      = os.environ["HEX_PROJECT_ID"]
HEX_INPUT_PARAM     = os.getenv("HEX_INPUT_PARAM_NAME", "team_leader_name")
HEX_BASE_URL        = os.getenv("HEX_BASE_URL", "https://app.hex.tech/api/v1")
HEX_RUN_TIMEOUT     = int(os.getenv("HEX_RUN_TIMEOUT", "300"))

SMTP_HOST           = os.environ["SMTP_HOST"]
SMTP_PORT           = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER           = os.environ["SMTP_USER"]
SMTP_PASSWORD       = os.environ["SMTP_PASSWORD"]
SENDER_NAME         = os.getenv("SENDER_NAME", "AI Usage Reports")
SENDER_EMAIL        = os.environ["SENDER_EMAIL"]

# ── Hex helpers ───────────────────────────────────────────────────────────────

def _hex_headers() -> dict:
    return {"Authorization": f"Bearer {HEX_API_TOKEN}", "Content-Type": "application/json"}


def run_hex_report(name: str) -> str:
    """
    Trigger a Hex project run filtered for *name* and return the run URL.
    Raises RuntimeError if the run fails or times out.
    """
    url = f"{HEX_BASE_URL}/projects/{HEX_PROJECT_ID}/runs"
    payload = {"inputParams": {HEX_INPUT_PARAM: name}}

    resp = requests.post(url, json=payload, headers=_hex_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    run_id  = data["runId"]
    run_url = data.get("runUrl", "")
    print(f"  [Hex] Run started – id={run_id}")

    # Poll for completion
    status_url = f"{HEX_BASE_URL}/projects/{HEX_PROJECT_ID}/runs/{run_id}"
    deadline = time.time() + HEX_RUN_TIMEOUT
    poll_interval = 10  # seconds

    while time.time() < deadline:
        time.sleep(poll_interval)
        status_resp = requests.get(status_url, headers=_hex_headers(), timeout=30)
        status_resp.raise_for_status()
        status = status_resp.json().get("status", "PENDING")
        print(f"  [Hex] Run status: {status}")

        if status == "COMPLETED":
            return run_url or status_resp.json().get("runUrl", "")
        if status in ("ERRORED", "KILLED", "CANCELLED"):
            raise RuntimeError(f"Hex run {run_id} ended with status: {status}")

    raise RuntimeError(f"Hex run {run_id} did not complete within {HEX_RUN_TIMEOUT}s")


# ── Email helpers ─────────────────────────────────────────────────────────────

def _build_email(to_name: str, to_email: str, run_url: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Weekly AI Usage Report – {to_name}"
    msg["From"]    = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"]      = to_email

    plain = (
        f"Hi {to_name},\n\n"
        f"Your weekly AI usage report is ready. View it here:\n{run_url}\n\n"
        f"This report shows AI usage metrics for your team for the past week.\n\n"
        f"Best regards,\n{SENDER_NAME}"
    )
    html = f"""\
<html><body>
<p>Hi <strong>{to_name}</strong>,</p>
<p>Your weekly AI usage report is ready.</p>
<p><a href="{run_url}" style="
   display:inline-block;padding:10px 20px;background:#e63131;
   color:#fff;text-decoration:none;border-radius:4px;font-weight:bold;">
   View My Report
</a></p>
<p>This report shows AI usage metrics for your team for the past week.</p>
<p>Best regards,<br><strong>{SENDER_NAME}</strong></p>
</body></html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg


def send_email(to_name: str, to_email: str, run_url: str) -> None:
    """Send the report email to *to_email*."""
    msg = _build_email(to_name, to_email, run_url)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.sendmail(SENDER_EMAIL, to_email, msg.as_string())
    print(f"  [Email] Sent to {to_email}")


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_and_send(name: str, email: str) -> None:
    """Full pipeline: run Hex report for *name* and email the result."""
    print(f"Processing report for {name} <{email}>")
    run_url = run_hex_report(name)
    send_email(name, email, run_url)
    print(f"Done – report sent to {email}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate and send the AI usage report for one team leader."
    )
    parser.add_argument("--name",  required=True, help="Team leader's full name")
    parser.add_argument("--email", required=True, help="Team leader's email address")
    args = parser.parse_args()

    generate_and_send(args.name, args.email)

