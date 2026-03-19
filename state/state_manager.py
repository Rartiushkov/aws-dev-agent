import json
import time


class StateManager:

    def save(self, goal, result, metadata=None):

        data = {
            "time": time.time(),
            "goal": goal,
            "result": result,
            "metadata": metadata or {}
        }

        with open("agent_state.json", "w") as f:
            json.dump(data, f, indent=2)
