# Synthetic dataset

Every company, person, account and document here is fictional. The documents were generated for this project (`backend/fixtures/generate.py`). They imitate the layout of Indian PAN cards, certificates of incorporation, GST registration certificates, Udyam (MSME) certificates and bank letters or cheques. None is a real government or bank document.

Each vendor folder has:
- `documents/`: the PDFs;
- `applications/`: one file per test scenario. Each has the form the supplier fills in (`form`), which document goes in which upload slot (`documents`), and the outcome the scenario is meant to produce (`expected_outcome`).

These are the same files the app's **Demo helper** loads, byte for byte (the app reads them from `backend/fixtures/out/`). The ground truth of what each document says is in `backend/fixtures/out/gold_labels.json`.

## Vendors and scenarios

| Vendor | Documents | Scenarios |
|---|---|---|
| **Nimbus Ledger Solutions Private Limited** (Bengaluru) | PAN, certificate of incorporation, GST certificate, bank letter, Udyam certificate; all digital and consistent | `happy_path`: every check passes, and Meridian approves it automatically. `code_of_conduct_declined`: rejected automatically by the policy rule. `code_of_conduct_unanswered`: the supplier is asked to answer. |
| **Tejas Facility Services Private Limited** | Cheque in the company's trade name; two GST certificates, one showing the trade name and one without it | `bank_account_in_trade_name_supported`: a reviewer judges it, with the GST trade name as evidence. `..._unsupported`: the supplier is asked to prove the account holder. |
| **Saffron Route Logistics Private Limited** | Company PAN and a director's personal PAN; the simulated GST registry shows three missed GSTR-3B returns | `director_pan_instead_of_company_pan`: the supplier is asked for the company PAN; the filing gaps go to finance. |
| **Indigo Pixel Studios LLP** | Old and new cancelled cheques; LLP certificate | `revision_1_certificate_missing`, then `revision_2_certificate_added_bank_changed`: submit both in order, the second as a correction. The certificate clears, and the bank change goes to finance for an independent confirmation. |
| **Harbourline Analytics Private Limited** | A GST certificate whose GSTIN is registered to a different company (Seaview Data Consultants, in the simulated registry) | `gstin_of_another_company`: the supplier is asked to explain; a reviewer can reject with a reason. |
| **Kaveri Cloudworks LLP** | Scanned PAN, a bank document mixing scanned and digital pages, and a scanned cheque with covered account digits | `scanned_documents_msme_without_certificate`: scanned values need a person to confirm them; MSME claimed without a certificate. `scanned_cheque_account_digits_covered`: the account can't be confirmed, and the supplier is asked for a clearer copy. |

## Other files

- **`test-aurora-fictional/`**: plain one-page documents (marked "TEST - FICTIONAL") made for testing the live site with documents the app has never seen. They have no prepared answers, so uploading them makes the app read them live, which takes 1–4 minutes per document. They include three bank letters for date checks:
  - one older than 6 months (it also names Bank of India for a State Bank of India IFSC);
  - one dated in the future;
  - one current.

  Fill in the form by hand from what the documents say.
- **`invalid-files/`**: a truncated PDF, a password-locked PDF, and a text file named `.pdf`. Each is refused at upload with a clear reason.
