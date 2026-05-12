#!/usr/bin/env python3

import json
import shlex
import subprocess
from pathlib import Path
from typing import Optional


PROJECT_DIR = Path("/Users/eyal.boumgarten/Documents/Projects/AI Usage")
TEAM_LEADERS_FILE = PROJECT_DIR / "team_leaders.json"
GENERATOR = PROJECT_DIR / "generate_all_reports.py"
PYTHON = PROJECT_DIR / ".venv" / "bin" / "python3"


def _load_tree() -> list[dict]:
    with TEAM_LEADERS_FILE.open() as fh:
        return json.load(fh)


def _flatten(nodes: list[dict]) -> list[dict]:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten(node.get("reports", [])))
    return result


def _subtree_names(node: dict) -> list[str]:
    return [person["name"] for person in _flatten([node])]


def _names_for_nodes(nodes: list[dict]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for node in nodes:
        for name in _subtree_names(node):
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                names.append(name)

    return names


def _tree_entries(tree: list[dict]) -> list[dict]:
    entries: list[dict] = []

    def walk(nodes: list[dict], depth: int) -> None:
        for node in nodes:
            kind = "root" if node.get("reports") else "leaf"
            marker = "[root]" if kind == "root" else "[leaf]"
            indent = "    " * depth
            number = len(entries) + 1
            entries.append({
                "label": f"{number:02d} {indent}{marker} {node['name']}",
                "node": node,
            })
            walk(node.get("reports", []), depth + 1)

    walk(tree, 0)
    return entries


def _open_terminal(command: str) -> None:
    applescript = f'''
tell application "Terminal"
  activate
  do script {command!r}
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)


def _build_generation_command(selected_names: list[str]) -> str:
    cmd = f"cd {shlex.quote(str(PROJECT_DIR))} || exit 1; {shlex.quote(str(PYTHON))} {shlex.quote(str(GENERATOR.name))}"
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
            'choose from list {"Choose From Tree", "Generate All"} with title "AI Usage Report Generator" with prompt "How would you like to generate reports?" default items {"Choose From Tree"} OK button name "Continue" cancel button name "Cancel" without multiple selections allowed',
            'end tell',
        ])
    except RuntimeError:
        return None


def _choose_tree_entries(entries: list[dict]) -> Optional[list[str]]:
    labels = [entry["label"] for entry in entries]
    applescript_labels = ", ".join(f'"{label.replace(chr(34), chr(92) + chr(34))}"' for label in labels)
    try:
        raw = _run_osascript([
            'tell application "System Events"',
            'activate',
            f'choose from list {{{applescript_labels}}} with title "AI Usage Report Generator" with prompt "Select roots or leaves. A root generates that whole subtree." OK button name "Generate" cancel button name "Cancel" with multiple selections allowed and empty selection allowed',
            'end tell',
        ])
    except RuntimeError:
        return None

    if raw == "false":
        return None
    if not raw:
        return []
    return [item for item in raw.split(", ") if item]


def _show_message(title: str, text: str) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display dialog {text!r} with title {title!r} buttons {{"OK"}} default button "OK"',
        ],
        check=False,
    )


def _choose_from_tree(tree_data: list[dict]) -> Optional[list[str]]:
    mode = _choose_mode()
    if not mode or mode == "false":
        return None
    if mode == "Generate All":
        return []

    entries = _tree_entries(tree_data)
    selected_labels = _choose_tree_entries(entries)
    if selected_labels is None:
        return None
    if not selected_labels:
        _show_message("No Selection", "Choose at least one root or leaf, or use Generate All.")
        return None

    selected = set(selected_labels)
    nodes = [entry["node"] for entry in entries if entry["label"] in selected]
    return _names_for_nodes(nodes)


def main() -> None:
    tree = _load_tree()
    selected_names = _choose_from_tree(tree)
    if selected_names is None:
        return

    _open_terminal(_build_generation_command(selected_names))


if __name__ == "__main__":
    main()
