#!/usr/bin/env python3
"""
Part 2 – Weekly scheduler: send AI usage reports to all team leaders.

The scheduler reads team_leaders.json (or the path in TEAM_LEADERS_FILE),
then runs send_report.generate_and_send() for every team leader on the
day and time configured in the .env file (SCHEDULE_DAY / SCHEDULE_TIME).

Usage:
    # Run the scheduler as a long-running process (e.g. in a screen session,
    # as a systemd service, or inside a Docker container):
    python scheduler.py

    # Or trigger one immediate run of all reports (useful for testing):
    python scheduler.py --run-now
"""

import argparse
import json
import logging
import os
import time

import schedule
from dotenv import load_dotenv

from send_report import generate_and_send

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
TEAM_LEADERS_FILE = os.getenv("TEAM_LEADERS_FILE", "team_leaders.json")
SCHEDULE_DAY      = os.getenv("SCHEDULE_DAY", "monday").lower()
SCHEDULE_TIME     = os.getenv("SCHEDULE_TIME", "08:00")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Team leader loading ───────────────────────────────────────────────────────

def load_team_leaders() -> list[dict]:
    """Load team leaders from the JSON file; raise a clear error if invalid."""
    if not os.path.exists(TEAM_LEADERS_FILE):
        raise FileNotFoundError(
            f"Team leaders file not found: {TEAM_LEADERS_FILE}\n"
            f"Create it or set TEAM_LEADERS_FILE in your .env."
        )
    with open(TEAM_LEADERS_FILE, encoding="utf-8") as fh:
        leaders = json.load(fh)

    if not isinstance(leaders, list):
        raise ValueError(f"{TEAM_LEADERS_FILE} must be a JSON array of objects.")
    for entry in leaders:
        if "name" not in entry or "email" not in entry:
            raise ValueError(
                f"Each entry in {TEAM_LEADERS_FILE} must have 'name' and 'email' fields.\n"
                f"Offending entry: {entry}"
            )
    return leaders


# ── Scheduled job ─────────────────────────────────────────────────────────────

def send_all_reports() -> None:
    """Generate and send reports for every team leader in the config file."""
    log.info("Starting weekly report run …")
    try:
        leaders = load_team_leaders()
    except (FileNotFoundError, ValueError) as exc:
        log.error("Failed to load team leaders: %s", exc)
        return

    log.info("Sending reports to %d team leader(s).", len(leaders))
    errors = []
    for leader in leaders:
        name  = leader["name"]
        email = leader["email"]
        try:
            generate_and_send(name, email)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to process %s <%s>: %s", name, email, exc)
            errors.append((name, email, str(exc)))

    if errors:
        log.warning("%d report(s) failed: %s", len(errors), errors)
    else:
        log.info("All reports sent successfully.")


# ── Schedule setup ────────────────────────────────────────────────────────────

_VALID_DAYS = {
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
}

def _register_schedule() -> None:
    """Register the weekly job according to SCHEDULE_DAY and SCHEDULE_TIME."""
    if SCHEDULE_DAY not in _VALID_DAYS:
        raise ValueError(
            f"SCHEDULE_DAY '{SCHEDULE_DAY}' is not valid. "
            f"Choose one of: {', '.join(sorted(_VALID_DAYS))}"
        )

    # schedule.<day>.do(job).at(time) pattern
    day_scheduler = getattr(schedule.every(), SCHEDULE_DAY)
    day_scheduler.at(SCHEDULE_TIME).do(send_all_reports)
    log.info(
        "Scheduler registered: reports will be sent every %s at %s.",
        SCHEDULE_DAY.capitalize(),
        SCHEDULE_TIME,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly AI usage report scheduler."
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Send all reports immediately (for testing), then exit.",
    )
    args = parser.parse_args()

    if args.run_now:
        log.info("--run-now flag detected; sending all reports immediately.")
        send_all_reports()
        return

    _register_schedule()
    log.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user.")


if __name__ == "__main__":
    main()

