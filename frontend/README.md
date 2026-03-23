# Local Frontend MVP

Static localhost landing page for the AWS Dev Agent product direction.

## Run locally

From the repo root:

```bash
python frontend/server.py
```

Then open:

`http://localhost:4173`

## Notes

- This localhost MVP now loads its action list and previews from the local Python backend.
- The backend reads from `bridges/ui_actions.py`.
- It is ready to be extended later with apply/status endpoints backed by `executor/ui_action_runner.py`.
- The visual direction intentionally mirrors the reference: blue atmospheric hero, command console,
  migration card, and CTA-driven product framing.
