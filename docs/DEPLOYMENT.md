# Deployment

The demo runs at https://verify-vendor-onboarding.onrender.com. It uses free tiers only, so most of this document is about making a free setup reliable.

| Piece | Where |
|---|---|
| App: one Docker web service, with the evaluation worker running inside it | Render, Singapore. `render.yaml` is applied as a Blueprint, and every push to `main` deploys. |
| Database | Neon PostgreSQL 17, AWS Singapore |
| Document reading | Gemini 3.8 Flash through its OpenAI-compatible endpoint, with NVIDIA and OpenRouter models as ordered backups |

## Settings

Secrets are set in the Render dashboard and never stored in the repository. The secrets are `DATABASE_URL`, `DEMO_ACCESS_CODE`, the model keys (`LLM_API_KEY`, `_2`, `_3`) and each backup's `LLM_FALLBACK*_BASE_URL`, `_MODEL` and `_API_KEY`. Render generates `SESSION_SECRET`. Every other setting, with its default, is in [`.env.example`](../.env.example) and `backend/app/config.py`.

`DEMO_ACCESS_CODE` puts the whole API behind one shared code. It protects the model quota from strangers; it is not user authentication. Changing it signs every browser out.

## What happens at start-up

1. **Migrations:** `alembic upgrade head`.
2. **Settings check:** production refuses to start with unsafe settings, such as a default session secret, the test-only reader, or a backup model with missing settings.
3. **PDF sandbox self-test:** the child process must parse a sample and report whether its memory cap is enforced.
4. **Re-evaluation of open cases** assessed under older rules. This uses the stored readings, so it makes no model calls.

`/api/health` answers `{"ok": true}` publicly and never touches the database. Behind the access code it also reports the reader, the state of each model key and the sandbox self-test.

## Making the free tier work

- **The free service doesn't wake reliably from sleep.** In testing it stayed unreachable for more than 13 minutes after sleeping. An outside monitor calls `/api/health` every 5 minutes to keep it awake. That uses about 720 of Render's 750 free instance hours a month.
- **Keeping the app awake must not keep the database awake.** Neon's free plan allows 100 compute-hours a month, and an always-on database would need about 180. So the health check doesn't query the database. The idle worker checks the queue once an hour, but wakes at once for new work, when a stopped run's lease expires, and within a minute of a database error.
- **512 MB of memory.** One PDF child at a time, capped at 450 MB. Images over 50 megapixels are refused before decoding. A 2.7 KB "decompression bomb" declaring a 20,000 × 20,000 image gets a clear `400 too_complex` in about 4 seconds.
- **Slow reads.** A document takes 54–234 seconds on the free model tier, and the provider sometimes answers `503 overloaded`. Each read has a 240-second timeout. After 90 seconds without an answer, it is handed over to the next model. Readings are cached by file hash, so the same file is never read twice.
- **Restarts mid-run.** A deploy that stops the instance while a run is reading is recovered: the new instance reclaims the run when its lease expires. Documents already read are reused, and the rest are read again.

## Verified on the hosted server

- **Linux memory cap:** enforced. A child capped at 200 MB had a 300 MB allocation refused; the real child peaks at about 155 MB.
- **Access gate:** without the code, API calls are refused and the health check says only "ok".
- **Flows:** the supplier and reviewer flows, the live run view of all six stages, the duplicate-account review and the manual registry task all work on the hosted site.
