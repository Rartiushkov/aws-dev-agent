import json
import os
import sys
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridges.ui_actions import build_action_preview, list_ui_actions
from executor.ui_action_runner import run_ui_action


FRONTEND_DIR = Path(__file__).resolve().parent
RUNS = {}


def _serialize_run(run):
    return {
        "run_id": run["run_id"],
        "action_id": run["action_id"],
        "status": run["status"],
        "approved": run["approved"],
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "result": run.get("result"),
        "error": run.get("error"),
    }


def _run_action_async(run_id):
    run = RUNS[run_id]
    run["status"] = "running"
    run["started_at"] = time.time()
    try:
      result = run_ui_action(run["action_id"], run["values"], approved=run["approved"])
      if result.get("status") == "approval_required":
          run["status"] = "approval_required"
      elif result.get("status") == "failed":
          run["status"] = "failed"
      else:
          run["status"] = "completed"
      run["result"] = result
    except Exception as exc:
      run["status"] = "failed"
      run["error"] = {"message": str(exc), "traceback": traceback.format_exc()}
    finally:
      run["finished_at"] = time.time()


class FrontendHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def _write_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/actions":
            actions = [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "description": item["description"],
                    "category": item["category"],
                    "preview_supported": item["preview_supported"],
                    "approval_required": item["approval_required"],
                }
                for item in list_ui_actions()
            ]
            return self._write_json(actions)
        if self.path.startswith("/api/runs/"):
            parsed = urlparse(self.path)
            run_id = parsed.path[len("/api/runs/") :]
            run = RUNS.get(run_id)
            if not run:
                return self._write_json({"error": "Run not found"}, status=404)
            return self._write_json(_serialize_run(run))
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/actions/"):
            return self._write_json({"error": "Not found"}, status=404)
        if self.path.endswith("/preview"):
            action_id = self.path[len("/api/actions/") : -len("/preview")]
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            values = payload.get("values") or {}
            try:
                preview = build_action_preview(action_id, values=values, apply=False)
            except Exception as exc:
                return self._write_json({"error": str(exc)}, status=400)
            return self._write_json(preview)
        if self.path.endswith("/apply"):
            action_id = self.path[len("/api/actions/") : -len("/apply")]
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            values = payload.get("values") or {}
            approved = bool(payload.get("approved"))
            try:
                build_action_preview(action_id, values=values, apply=True)
            except Exception as exc:
                return self._write_json({"error": str(exc)}, status=400)
            run_id = str(uuid.uuid4())
            RUNS[run_id] = {
                "run_id": run_id,
                "action_id": action_id,
                "values": values,
                "approved": approved,
                "status": "queued",
                "created_at": time.time(),
            }
            thread = threading.Thread(target=_run_action_async, args=(run_id,), daemon=True)
            thread.start()
            return self._write_json(_serialize_run(RUNS[run_id]), status=202)
        if self.path.endswith("/status"):
            action_id = self.path[len("/api/actions/") : -len("/status")]
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            values = payload.get("values") or {}
            try:
                preview = build_action_preview(action_id, values=values, apply=False)
            except Exception as exc:
                return self._write_json({"error": str(exc)}, status=400)
            return self._write_json(preview.get("status", {}))
        return self._write_json({"error": "Not found"}, status=404)


def main():
    port = int(os.environ.get("FRONTEND_PORT", "4173"))
    server = ThreadingHTTPServer(("127.0.0.1", port), FrontendHandler)
    print(f"Frontend server running on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
