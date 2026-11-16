---
name: caf-cli
description: Read the user's caf.fr "Mon Compte" (Caisse d'Allocations Familiales) from the terminal with the `caf` command — payments, monthly entitlements (droits) with the reason a benefit is refused, letters and emails (« Mes courriers, courriels »), online procedures (démarches), the household profile — and download letters, procedure receipts, attestations (paiement, quotient familial, montant net social) and the monthly relevé de compte as PDF; email the CAF through « Contacter ma Caf » (dry run unless --yes). Drives a dedicated Chrome over CDP because caf.fr sits behind a Radware bot manager. Use when the user asks about their CAF payments or allocations, needs an attestation CAF / quotient familial, wants a CAF letter, or wants to write to the CAF. Triggers - "caf", "caf.fr", "mes paiements caf", "attestation de paiement caf", "attestation quotient familial", "quotient familial", "courrier caf", "écris à la caf", "relevé caf", "mes droits caf", "allocations familiales", "CMG", "Paje".
---

# caf-cli

Your caf.fr Mon Compte through the private JSON API behind its Angular apps.

## How to invoke

`caf` is on `$PATH` via `uv run claudine/scripts/link_clis.py`, which links
`bin/caf` into `~/.local/bin`. If it is missing, run `<skill-dir>/bin/caf`
directly: it is a PEP 723 script and `uv` resolves its pinned dependencies.
Every command takes `--help` with examples.

## Authentication

No credential is stored. The CLI drives a dedicated Chrome (profile
`~/.caf-chrome`, DevTools port 9225, started on demand) that keeps the caf.fr
cookies between runs. Every API call is a `fetch` inside a signed-in
wwwd.caf.fr tab, with a 5-minute JWT the portal mints at
`/wps/s/GenerateTokenJwt/`.

- Not signed in → every command fails with
  ``Not signed in to caf.fr — run `caf login` …``. Ask the user to run
  `! caf login`: it opens the window and waits (300 s) while they sign in
  (numéro allocataire + mot de passe, or FranceConnect). The person types the
  password; an agent never does.
- `caf whoami` proves the binding (name, CAF code, dossier number).
- Overrides: `CAF_CDP_URL`, `CAF_CHROME_PROFILE`, `CAF_DOWNLOAD_DIR`.

## Output conventions

- Default: one line per record, tab-separated. In `courriers` and `demarches`
  the id the next command takes comes first; amounts are `38,06 €`.
- `--json`: normalized records (ISO dates, `*_cents` integers, stable keys).
  `--raw` (reads only): the API's answer untouched.
- Logs and warnings go to stderr; stdout carries data only.
- PDFs go to `~/Downloads/caf/` (or `--target-dir`, `-o`); the absolute path
  is printed.
- Social security numbers (NIR) are never printed, even though the profile API
  returns them. Use `--raw` only for debugging, never to show a profile.

## Commands

```bash
# Read
caf whoami --json                      # name, CAF code, dossier number, unread flag
caf profil                             # household members, situation, address
caf paiements --limit 5                # payments, latest first, with the benefits each covers
caf paiements --since 2026-01 --json
caf droits                             # this month's entitlements
caf droits --months 6 --no-entitlement # + refused benefits and the reason
caf droits --month 2026-03 --json
caf courriers                          # letters, emails, declarations (id first)
caf courriers --kind courrier --limit 5
caf courriers --unread
caf courrier A751000000000A0000000A    # an email's full text and status; a letter's PDF address
caf demarches                          # online procedures; id = receipt PDF address
caf topics                             # subject codes for `caf send`

# Local writes (PDF downloads)
caf download A751000000000A0000000A    # a letter, by courrier id…
caf download 751T0000000000000000000PDF 751O0000000000000000000PDF --target-dir ~/CAF
caf courrier A751000000000A0000000A --save
caf attestation paiements              # latest attestation de paiement
caf attestation paiements --from 2026-01 --to 2026-10
caf attestation qf -o qf.pdf           # attestation de quotient familial
caf attestation net-social             # montant net social
caf releve                             # latest monthly relevé de compte

# Outward write
caf send --topic INFO_DOS --body-file q.txt          # DRY RUN: prints the exact request
caf send --topic INFO_DOS --body-file q.txt --yes    # really emails the CAF
caf send --topic INFO_REC --motif RECL_PAI --prestation PRES_PAJ --body-file q.txt --yes
caf login                              # terminal only: wait for a manual sign-in
```

## Recipes

- **"What did the CAF pay me this year?"** `caf paiements --since 2026-01 --json`
  and sum `amount_cents`.
- **"Why don't I get benefit X?"** `caf droits --no-entitlement`: the reason is
  on each "sans droit" line (e.g. resources over the ceiling, Pajemploi salary
  declaration missing).
- **Attestation for a crèche, school or landlord:** `caf attestation qf`
  (quotient familial) or `caf attestation paiements --from … --to …`.
- **Latest letter from the CAF:** `caf courriers --kind courrier --limit 1`,
  then `caf download <id>` and read the PDF.
- **Write to the CAF:** pick a code with `caf topics`, draft the text in a file
  (≤ 3000 characters), run `caf send …` without `--yes`, show the dry run to
  the user, and add `--yes` only after they approve. The email then shows in
  `caf courriers --kind courriel`.
- **Archive everything:** `caf courriers --kind courrier --json` and
  `caf demarches --json`, then one `caf download id1 id2 …` call.

## Gotchas

- Plain HTTP clients get a 302 to a Radware JS challenge on every
  wwwd.caf.fr path, even with a valid bearer token, so all calls go through
  Chrome. Copying cookies out of a browser does not work either.
- Measured (2026-11, one account): the payment list covers the last 6 months
  (10 payments); older ones are only in `caf attestation paiements --from …`.
  `droits` covers 24 months, `courriers` 2 years (29 items), `demarches` 12
  procedures. Each list is one unpaginated call; a command takes ~0.4 s.
- `courriers` mixes three kinds: `courrier` (a letter from the CAF, PDF),
  `courriel` (an email you sent: its text and the CAF's status line; the CAF's
  real answer usually arrives as a separate `courrier`), and `declaration`
  (acknowledgement of a declaration you made; no content beyond the status).
  Declarations have no id on caf.fr; the CLI gives them a stable `D…` hash.
- The API answers 200 with sections missing when a back end is down
  (`courriersEnErreur`, …); the CLI then warns on stderr that the list may be
  incomplete. Retry later rather than concluding there is nothing.
- Reading never marks a letter read on caf.fr (the CLI does not call the
  `…/mon_compte/lu` endpoint the website calls when you open one).
- The `send` topic list mirrors the signed-in form's list in caf.fr's
  contactermacafappli bundle; if caf.fr changes it, `send` fails with the API's
  error. The website's "Camille" assistant (suggested answers before sending)
  is skipped.
- A period attestation that includes the current, unclosed month answers 409
  for `net-social` (paiements accepts it); end `--to` at the previous month.
  Periods before the account's history (measured: before 2025 here) come back
  as an attestation saying "aucun paiement", which is not proof of no payment.
- `courriers` subjects for letters are always "Courrier de votre Caf"; the real
  subject is only in the PDF (`pdftotext` its first lines to name the file).
- `caf send --yes` is not idempotent: running it twice sends two emails.
