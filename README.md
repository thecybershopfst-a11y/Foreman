# Foreman

Finance, invoicing, CRM, pipeline, and inventory — in one self-contained HTML file. No backend, no server, no account. Open the file, it runs entirely in your own browser.

## What's in this repo

| File | What it is |
|---|---|
| `index.html` | The public sales page — foreman-os.ca points here |
| `business-os.html` | **The actual product.** This is the file customers receive and open. |
| `foreman-demo.html` | Live, pre-loaded demo of `business-os.html` — no purchase needed, nothing saved |
| `download.html` | The page linked from the delivery email. Fetches `business-os.html` and forces a real file download (a plain link to `business-os.html` doesn't trigger a download in most browsers) |

## How the app works

`business-os.html` saves everything to `localStorage` in the customer's own browser — nothing is sent to any server, because there isn't one. It detects whether it's running as a Claude.ai artifact (which provides a `window.storage` API) or standalone in a regular browser tab, and falls back to `localStorage` automatically in the standalone case.

The Marketing, Documents, and Supervisor features work by generating a ready-to-use prompt inside the app, which the customer copies into their own Claude account to get a draft, then pastes back in. There's no API key baked into the app and no backend call — each customer uses their own Claude account for the AI parts.

## How it's sold

- **Checkout:** a live Stripe Payment Link (not embedded in this repo — see your Stripe dashboard)
- **Price:** $99 CAD introductory, rising to $199 for new customers after the introductory period ends; anyone who buys at $99 keeps that price permanently
- **Fulfillment is manual:** after a sale comes through on Stripe, send the customer an email with a link to `download.html` — that page pulls the current `business-os.html` and forces a real download
- **Demo:** `foreman-demo.html` is public and needs no purchase — linked from the sales page for anyone to try first

## Updating the product

Since `business-os.html` is the file customers actually download, any change to it goes live for new purchasers the moment you upload the new version — no build step, no redeploy. `download.html` always fetches whatever is currently on `main`, so it never needs to change when the app does.

## Legacy files — not currently used

`main.py`, `render.yaml`, `requirements.txt`, `scheduled_automations.py`, and `.github/workflows/` are left over from an earlier version of Foreman that ran as a hosted Python backend on Render with a database. That approach was retired in favor of the single-file, no-backend design above. These files aren't referenced by anything customer-facing and can be safely deleted whenever you want the repo tidied up — just say the word.
