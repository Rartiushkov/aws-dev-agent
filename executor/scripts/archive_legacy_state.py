import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


LEGACY_NAMES = [
    "aws_inventory",
    "deployments",
    "migrations",
    "audit",
    "agent_memory",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Archive legacy shared state into a timestamped folder.")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def build_plan():
    state_dir = Path("state")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = state_dir / "legacy_global_archive" / timestamp
    moves = []
    for name in LEGACY_NAMES:
        source = state_dir / name
        if source.exists():
            moves.append({"source": str(source), "target": str(archive_root / name)})
    agent_state = Path("agent_state.json")
    if agent_state.exists():
        moves.append({"source": str(agent_state), "target": str(archive_root / "agent_state.json")})
    return archive_root, moves


def execute_plan(archive_root, moves):
    archive_root.mkdir(parents=True, exist_ok=True)
    for item in moves:
        source = Path(item["source"])
        target = Path(item["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def main():
    args = parse_args()
    archive_root, moves = build_plan()
    if args.execute:
        execute_plan(archive_root, moves)
    report = {
        "status": "ok",
        "executed": args.execute,
        "archive_root": str(archive_root),
        "move_count": len(moves),
        "moves": moves,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
