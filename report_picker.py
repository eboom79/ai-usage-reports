#!/usr/bin/env python3

import json
import shlex
import subprocess
from pathlib import Path
from typing import Optional


PROJECT_DIR = Path("/Users/eyal.boumgarten/Documents/Projects/AI Usage")
TEAM_LEADERS_FILE = PROJECT_DIR / "team_leaders.json"
GENERATOR = PROJECT_DIR / "generate_all_reports.py"


def _load_names() -> list[str]:
    def flatten(nodes):
        result = []
        for node in nodes:
            result.append(node["name"])
            result.extend(flatten(node.get("reports", [])))
        return result

    with TEAM_LEADERS_FILE.open() as fh:
        return flatten(json.load(fh))


def _open_terminal(command: str) -> None:
    applescript = f'''
tell application "Terminal"
  activate
  do script {command!r}
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)


def _build_generation_command(selected_names: list[str]) -> str:
    cmd = f"cd {shlex.quote(str(PROJECT_DIR))} || exit 1; python3 {shlex.quote(str(GENERATOR.name))}"
    for name in selected_names:
        cmd += f" --name {shlex.quote(name)}"
    cmd += (
        "; exit_code=$?; echo; "
        "if [ $exit_code -eq 0 ]; then echo 'Report generation finished successfully.'; "
        "else echo 'Report generation exited with status '$exit_code'.'; fi; "
        "echo 'Press Return to close this window.'; read"
    )
    return cmd


def _run_osascript(lines: list[str]) -> str:
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def _choose_mode() -> Optional[str]:
    try:
        return _run_osascript([
            'tell application "System Events"',
            'activate',
            'choose from list {"Generate All", "Choose Team Leaders"} with title "AI Usage Report Generator" with prompt "How would you like to generate reports?" default items {"Choose Team Leaders"} OK button name "Continue" cancel button name "Cancel" without multiple selections allowed',
            'end tell',
        ])
    except RuntimeError:
        return None


def _choose_names(names: list[str]) -> Optional[list[str]]:
    applescript_names = ", ".join(f'"{name.replace(chr(34), chr(92) + chr(34))}"' for name in names)
    try:
        raw = _run_osascript([
            'tell application "System Events"',
            'activate',
            f'choose from list {{{applescript_names}}} with title "AI Usage Report Generator" with prompt "Select one or more team leaders." OK button name "Generate" cancel button name "Cancel" with multiple selections allowed and empty selection allowed',
            'end tell',
        ])
    except RuntimeError:
        return None

    if raw == "false":
        return None
    if not raw:
        return []
    return [item.strip() for item in raw.split(", ") if item.strip()]


def _show_message(title: str, text: str) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display dialog {text!r} with title {title!r} buttons {{"OK"}} default button "OK"',
        ],
        check=False,
    )


def main() -> None:
    names = _load_names()
    mode = _choose_mode()
    if not mode or mode == "false":
        return

    if mode == "Generate All":
        _open_terminal(_build_generation_command([]))
        return

    selected = _choose_names(names)
    if selected is None:
        return
    if not selected:
        _show_message("No Selection", "Choose at least one team leader, or use Generate All.")
        return

    _open_terminal(_build_generation_command(selected))


if __name__ == "__main__":
    main()
