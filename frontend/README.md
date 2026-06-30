# Product Landing

Static launch landing page for the Availabl product direction.

## Run locally

From the repo root:

```bash
python frontend/server.py
```

Then open:

`http://localhost:4173`

## Notes

- The landing page is now fully static and does not depend on the local `/api` routes.
- It is designed for a Product Hunt style launch and can be deployed directly to GitHub Pages or Cloudflare Pages.
- The current message is intentionally narrow: supported AWS environment cloning into a new account or region.

## Cloudflare Pages

Recommended project settings:

- Production branch: `main`
- Root directory: `frontend`
- Build command: leave empty
- Build output directory: `.`

The `frontend/_headers` file keeps the same security headers that were previously set in `render.yaml`.
