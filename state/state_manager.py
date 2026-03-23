import json
import time

from executor.scripts.transfer_common import resolve_client_slug, state_root


class StateManager:

    def save(self, goal, result, metadata=None, client_slug=""):
        resolved_client_slug = resolve_client_slug(client_slug, source_env=(metadata or {}).get("source_env", ""), target_env=(metadata or {}).get("target_env", ""))

        data = {
            "time": time.time(),
            "goal": goal,
            "result": result,
            "metadata": metadata or {},
            "client_slug": resolved_client_slug,
        }

        target_path = state_root(resolved_client_slug) / "agent_state.json"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
