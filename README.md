# Verify: supplier onboarding for Indian buyers

Verify takes a supplier's onboarding application (a form and its PDF documents) and decides whether the buyer can approve the supplier. Every application ends **approved**, **pending** or **rejected**. A pending one says exactly what is still needed and who owes it: the supplier, a procurement reviewer or finance. The supplier is told in plain language what to fix.

**Live demo:** https://verify-vendor-onboarding.onrender.com. Open it and pick a role; there is nothing to sign up for. All companies, people and documents in it are fictional.

**Cached readings:** the sample documents on the live site were read by the model in advance, and the model's answers are stored with them, matched to each file by its hash. So the Demo helper's samples run in seconds without calling the model. A document the site has not seen before is read live, which takes about 1–4 minutes on the free model tier.

Scope: Indian private limited companies and LLPs supplying services to a buyer. Other entity types are routed to a reviewer as out of scope, never rejected.

## What happens to an application

Each submission runs through six stages. The live run view shows each one as it happens.

1. **Receive.** The form and PDFs are stored as one revision, which can't be changed afterwards. Locked, broken, oversized and non-PDF files are refused at upload.
2. **Read.** A vision model reads each document on its own and decides its type from the content. **The model never sees the form**, so it can't echo what the supplier typed. Every value it returns has a status (found, not found, unreadable, ambiguous), a page and a quote.
3. **Compare.** The form is compared with every document: legal name, PAN, GSTIN, CIN, bank account, IFSC and more. If a correction was submitted, it is also compared with the revision a person last cleared.
4. **Check.** The buyer's policy, Indian ID rules and the sources are applied:
   - **ID rules:** for example, the GSTIN must contain the PAN, the PAN's 4th letter must match the entity type, and the check digit must be valid.
   - **Sources:** the GST registry, the IFSC directory, a bank account check, and digital signatures on certificates.
   - **Risk:** a 0–100 score built from named red flags.
5. **Decide.** Each open item gets one owner. A clean, low-risk application is approved automatically when the buyer's policy allows it. A high-risk one needs two different people to approve.
6. **Communicate.** The supplier sees only their own items, written for them. Reviewers see the evidence, the reasoning and the history.

Approval creates the supplier's approved profile and a **vendor master record** (JSON or CSV) for the finance system. The record includes the GST and MSME payment terms, a TDS suggestion for the tax team, how the bank account was checked, and a payment hold until the account is confirmed at a live source.

## Design choices that matter

- **The system never rejects on its own judgement.** It rejects automatically only on an explicit policy rule: today, a supplier declining the code of conduct. Anything uncertain goes to a named person, with the evidence.
- **A value counts as proven only if our code finds it in the page's own text layer.** A value read only from an image, such as a scan, needs a person to confirm it. Text hidden in a file (white on white, covered digits) is detected and never counts as proof.
- **A person's judgement is bound to the exact evidence it saw.** If the supplier later changes that evidence, the judgement no longer counts and the item returns to the reviewer.
- **Bank changes are treated as the main payment-fraud risk.** Finance can accept a changed account only after confirming it through a contact the buyer already held, never one taken from the application. The person who confirmed it can't also give the final approval.
- **Duplicates are caught across suppliers.** The same bank account, PAN or GSTIN already on file for another supplier of the same buyer goes to a reviewer.
- **Decisions can't be made on stale facts.** Every write carries the version of the screen the person saw. Approval re-checks everything under database locks, and an assessment made under older rules is evaluated again first.

## Try it

The supplier form has a **Demo helper** with prepared synthetic applications. Their documents are real PDFs that have already been read, so they run in seconds without calling a model. The same documents and application forms are in [datasets/](datasets/), one folder per fictional vendor.

**Happy path (Nimbus Ledger).** Every check passes. Meridian approves it automatically; at Kestrel a reviewer approves it.

### The four main edge cases

No single-field check catches these. Each needs two or more sources compared, or the application's history.

| Edge case | What the system does |
|---|---|
| **Bank account in a trade name.** The account holder is "TEJAS CLEANPRO", not the company's legal name. There are two samples: with and without supporting evidence. | If the GST certificate lists that trade name, a reviewer judges it with the certificate shown as evidence. Without it, the supplier is asked to prove whose account it is. |
| **A director's personal PAN instead of the company's.** Every ID is valid on its own. | The PAN's 4th letter marks an individual, while the GSTIN and the incorporation certificate carry the company PAN. The supplier is asked for the company PAN and told where it appears. Unfiled GST returns go to finance. |
| **A correction that also changes the bank account.** Revision 1 is missing the LLP certificate; revision 2 adds it and changes the account. | The certificate clears. The bank change goes to finance, who must confirm it through a contact the buyer already held. The person who confirmed it can't give the final approval. |
| **A GSTIN registered to another company.** The format, check character and registration status are all valid. | The registry lists a different business for it. The supplier is asked to explain, and a reviewer can reject with a reason. |

### Other samples

| Sample | What it shows |
|---|---|
| Scanned documents, MSME claimed without a certificate | Scanned values need a person to confirm them. Meridian requires the MSME proof, Kestrel doesn't. |
| Scanned cheque with covered account digits | The account can't be confirmed; the supplier is asked for a clearer copy. |
| Code of conduct declined / left unanswered | Rejected automatically by the policy rule / the supplier is asked to answer. |

Running the happy path twice for the same buyer sends the second application to a duplicate-supplier review. [datasets/invalid-files/](datasets/invalid-files/) has files that are refused at upload.

Sign in from the start page as a supplier, a procurement reviewer or a finance reviewer for one of two fictional buyers:
- **Meridian Retail India:** approves clean, low-risk applications automatically, and uses a simulated GST registry.
- **Kestrel Health Systems:** a person approves every application and records the GST registry lookup by hand.

## What is real and what is simulated

| Source | In this demo | In production |
|---|---|---|
| Document reading | Real model calls: Gemini 3.8 Flash, with NVIDIA and OpenRouter models as backups. Readings are cached by file hash. | Same, on a paid tier |
| Digital signatures | Real cryptographic check, offline. The chain to the CCA India root is not checked. | Pin the CCA roots |
| IFSC directory | Live (Razorpay's public RBI IFSC directory) | Same |
| GST registry | Simulated test registry, or a lookup a reviewer records from the GST portal | GSTN through a GST data provider |
| Bank account check (penny drop) | Sandbox: only the sample accounts are known. Anything else is "no record", never "verified". | A payment provider's account-validation API |
| PAN, MCA company status, Udyam | Not checked at the source: formats, cross-checks and the documents only | Provider APIs |

The interface labels every simulated or sandbox result as such.

## Architecture

FastAPI and PostgreSQL on the backend, with a single background worker that claims runs from the database. React 19, Vite and Tailwind on the frontend. PDFs are parsed in a separate child process with a memory cap. The evaluation itself is a set of pure functions (inputs in, findings out, no database and no model), which is where most of the tests are.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers the components, data model, checks, routing and states, and how this compares with real Indian onboarding practice. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) covers the hosted setup.

## Run it locally

Built and tested with Python 3.14, Node 24 and PostgreSQL 17 (the same versions as the Docker image and the hosted database). On Windows, `scripts/start_db.ps1` starts a portable PostgreSQL on port 55432.

```bash
# backend
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements.txt    # .venv/bin/pip on macOS/Linux
cp .env.example .env                                      # then set DATABASE_URL and, for live reading, LLM_API_KEY

# frontend (served by the backend once built)
cd frontend && npm ci && npm run build && cd ..

# start: runs the migrations, then serves on http://127.0.0.1:8000
.venv/Scripts/python backend/scripts/serve.py --fixture-reader
```

`--fixture-reader` reads the sample documents from prepared answers instead of calling a model, so no API key is needed. Leave it out to read documents live.

## Tests

```bash
cd backend && ../.venv/Scripts/python -m pytest -q
```

332 tests run against a real PostgreSQL test database: `vendor_onboarding_test`, or the database in `TEST_DATABASE_URL`, which the tests reset. They make no model calls and no network calls. Pure-logic tests run each sample through compare, checks and routing. API tests drive full submissions, corrections, reviews and approvals, including races between a correction and an approval.

## Repository layout

```
backend/
  app/
    api/          routes, sign-in, an optional access gate
    services/     commands (every write) and views (separate supplier and staff pages)
    pipeline/     worker, runner, compare, checks, risk, routing, approval, vendor master record
    extraction/   model client with backups, reading contract, cache
    pdf/          sandboxed PDF intake, page rendering, signature checks
    verification/ IFSC directory and bank account check
    india/        PAN, GSTIN, CIN, IFSC, Udyam and PIN validators
  policies/       buyer policies (JSON, versioned)
  fixtures/       generator for the sample PDFs, and the prepared answers
  migrations/     Alembic
  tests/
frontend/src/     supplier portal, reviewer dashboard, case view, live run view
datasets/         the synthetic vendors: their documents and application forms
docs/             architecture and deployment
```

## Limits

- **Sign-in is a persona picker with no passwords.** That's enough for a demo, not for production.
- **Fresh documents are slow to read on the free model tier,** about 1–4 minutes each. The samples are cached.
- **Two of the source checks are simulated,** the GST registry and the bank account check, and PAN, MCA and Udyam are not checked at the source. Production would call paid providers.
- **Not built:** e-mail reminders, periodic re-verification, employee conflict-of-interest matching, and a direct push into an ERP system. The vendor master record is exported instead.
