#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "websocket-client==1.9.2",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""caf-cli — your caf.fr "Mon Compte" (Caisse d'Allocations Familiales) from the terminal.

Reads payments, entitlements (droits), letters and emails, online procedures
(démarches) and the household profile; downloads letters, procedure receipts,
attestations (paiement, quotient familial, montant net social) and the monthly
account statement as PDF; sends an email to the CAF (dry run unless --yes).

Why a browser: wwwd.caf.fr sits behind a Radware bot manager that answers any
non-browser HTTP client, bearer token or not, with a 302 to a JS challenge. The
Angular apps of Mon Compte call a private JSON API (`/api/<app>front/v1/...`)
with a 5-minute JWT minted by the portal at `/wps/s/GenerateTokenJwt/`. So this
CLI attaches to a dedicated Chrome over the DevTools Protocol (CDP) and runs
every call as a `fetch` inside a signed-in wwwd.caf.fr tab.

The dedicated Chrome (profile ~/.caf-chrome, port 9225) is launched on demand
and keeps its cookies between runs; `caf login` opens it on the login page and
waits for the person to sign in (CAF credentials or FranceConnect).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import click
import websocket  # websocket-client

log = logging.getLogger("caf")

ORIGIN = "https://wwwd.caf.fr"
HOME_URL = f"{ORIGIN}/wps/myportal/caffr/moncompte/tableaudebord"
LOGIN_HOST = "connect.caf.fr"
TOKEN_PATH = "/wps/s/GenerateTokenJwt/"
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
MAX_RETRIES = 3
FETCH_TIMEOUT_MS = 60_000
# The contact form's own limit (explicationMaxSize in contactermacafappli).
MAX_MESSAGE_CHARS = 3000
# Control characters the contact form strips before sending.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

API = {
    "paiements": "/api/paiementsfront/v1/mon_compte/paiements",
    "droits": "/api/droitsfront/v1/mon_compte/droits",
    "courriers": "/api/courrierscourrielsfront/v1/mon_compte/liste_courriers_courriels",
    "non_lus": "/api/courrierscourrielsfront/v1/mon_compte/messages_non_lus",
    "demarches": "/api/demarchesenlignefront/v1/mon_compte/liste_demarches_en_ligne",
    "profil_reduit": "/api/profilreduitfront/v1/mon_compte/profil_reduit",
    "profil": "/api/profilcompletfront/v1/profilcalp",
    "pdf": "/api/fluxelecfront/v1/mon_compte/read_pdf/",
    "releve": "/api/rmsfront/v1/mon_compte/rms",
    "attestation_last": "/api/attestationsfront/v1/mon_compte/derniere_attestation/",
    "attestation_period": "/api/attestationsfront/v1/mon_compte/attestation_sur_periode/",
    "pages_locales": "/api/editorialmiddle/v2/mon_compte/pages_locales",
    "coordonnees": "/api/contactercaffront/v1/mon_compte/contacter_caf/coordonnees_contact",
    "courriel_autorise": "/api/contactercaffront/v1/mon_compte/contacter_caf/autorisation_courriel",
    "envoi_mail": "/api/contactercaffront/v1/mon_compte/contacter_caf/envoi_mail",
}

# Attestation kinds: CLI name -> (API suffix, label).
ATTESTATIONS = {
    "paiements": ("paiements", "Attestation de paiement"),
    "qf": ("qf", "Attestation de quotient familial"),
    "net-social": ("mns", "Attestation montant net social"),
}

# Email subjects of the signed-in contact form (listeObjetMessage in
# contactermacafappli/dist/main.js): code -> (label, cleRnProduit).
TOPICS = {
    "INFO_ALF": ("Informations sur les allocations pour les enfants", "RN_PRODUCT_122"),
    "INFO_ETU": ("Étudiant – Demande d'information aides personnelles au logement", "RN_PRODUCT_118"),
    "INFO_APL": ("Informations sur les aides personnelles au logement", "RN_PRODUCT_121"),
    "INFO_RSA": ("Information sur le Rsa", "RN_PRODUCT_119"),
    "INFO_PPA": ("Information sur la prime d'activité", "RN_PRODUCT_117"),
    "INFO_AUT": ("Informations sur l'aide d'urgence pour les victimes de violences conjugales", "RN_PRODUCT_566"),
    "INFO_AAH": ("Je m'informe sur les aides handicap/proche aidant", "RN_PRODUCT_120"),
    "INFO_DOS": ("Question sur le suivi de votre dossier", "RN_PRODUCT_125"),
    "INFO_CHT": ("Question suite à une déclaration de changement de situation", "RN_PRODUCT_113"),
    "INFO_ERR": ("Oups ! j'ai fait une erreur, que faire ?", "RN_PRODUCT_299"),
    "INFO_ATT": ("Demande d'attestation de paiement", "RN_PRODUCT_114"),
    "INFO_PAI": ("Contester un droit, un paiement ou un indu", "RN_PRODUCT_112"),
    "INFO_CRE": ("Question sur vos remboursements/retenues. Demande de remise de dettes ou un échéancier",
                 "RN_PRODUCT_124"),
    "INFO_ACC": ("Demande d'information sur les points d'accueil et les modes de contacts", "RN_PRODUCT_116"),
    "INFO_CAF": ("Demande d'information sur le fonctionnement ou l'actualité de votre Caf", "RN_PRODUCT_115"),
    "INFO_SIT": ("Aide à l'utilisation du site (Hotline)", "RN_PRODUCT_110"),
    "INFO_REC": ("Dépôt d'une réclamation", "RN_PRODUCT_123"),
}
# A réclamation (INFO_REC) also needs a motif, and a prestation for the file-related motifs.
RECLAMATION_MOTIFS = {
    "RECL_PAI": ("Vous n'avez pas reçu votre paiement", "dossier"),
    "RECL_DEM": ("Vous n'avez pas eu de réponse à votre demande", "dossier"),
    "RECL_DOC": ("Les documents transmis n'ont pas été pris ou mal en compte", "dossier"),
    "RECL_CAF": ("Vous ne comprenez pas la décision de la Caf", "dossier"),
    "RECL_CRE": ("Vous ne comprenez pas votre trop perçu ou les modalités de remboursement", "dossier"),
    "RECL_INS": ("Vous n'êtes pas satisfait des conditions d'accueil", "caf"),
    "RECL_TEL": ("Vous avez des difficultés à joindre la Caf au téléphone", "caf"),
    "RECL_SIT": ("Vous n'êtes pas satisfait du site Caf.fr", "caf"),
    "RECL_ACC": ("Vous n'êtes pas satisfait de l'accueil du personnel", "caf"),
}
PRESTATIONS = {
    "PRES_APL": "Aides au logement",
    "PRES_RSA": "Rsa",
    "PRES_PPA": "Prime d'activité",
    "PRES_AAH": "Aah",
    "PRES_PAJ": "PAJE/ Compléments de mode de garde",
    "PRES_ALF": "Allocations familiales/ Complément familial",
    "PRES_ARS": "Allocation de rentrée scolaire",
    "PRES_AJP": "Allocation journalière de présence parentale",
    "PRES_ASF": "Allocation de soutien familial",
    "PRES_AEH": "Allocation d'éducation de l'enfant handicapé",
}

# courriersCourriels.typeMessage -> CLI kind.
MESSAGE_KINDS = {"courriercaf": "courrier", "mailcaf": "courriel", "courrieralloc": "declaration"}

NOT_LOGGED = ("Not signed in to caf.fr — run `caf connect` (the connect tool in Claude): it opens a Chrome "
              "window on the CAF login page; sign in there, then retry.")


def env_value(name: str) -> str:
    """An env var, treating an unfilled extension setting (`${user_config.x}`) as unset."""
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("${") else value


def default_out() -> Path:
    """$CAF_DOWNLOAD_DIR, else ~/Downloads/caf."""
    value = env_value("CAF_DOWNLOAD_DIR")
    return Path(value).expanduser() if value else Path.home() / "Downloads" / "caf"


DEFAULT_CDP = env_value("CAF_CDP_URL") or "http://localhost:9225"
PROFILE_DIR = Path(env_value("CAF_CHROME_PROFILE") or Path.home() / ".caf-chrome")

# Runs one API call in the page: mints a JWT (cached until 30 s before expiry),
# fetches with a timeout, returns {status, ct, cd, text|b64}.
FETCH_JS = """(async()=>{
 const path=__PATH__, opts=__OPTS__, binary=__BINARY__;
 const now=Date.now()/1000;
 if(!window.__cafTok || window.__cafTok.exp-30<now){
   const r=await fetch(__TOKEN__,{headers:{'X-Requested-With':'XMLHttpRequest'}});
   const ct=r.headers.get('content-type')||'';
   if(!r.ok || !ct.includes('json')) return JSON.stringify({status:401, ct, text:'no token'});
   const tok=(await r.json()).cnafTokenJwt;
   const claims=JSON.parse(atob(tok.split(' ')[1].split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
   window.__cafTok={tok, exp:claims.exp};
 }
 const ctl=new AbortController(); const t=setTimeout(()=>ctl.abort(), __TIMEOUT__);
 const headers=Object.assign({Authorization:window.__cafTok.tok, Accept:'application/json, text/plain, */*'}, opts.headers||{});
 let r;
 try { r=await fetch(path,{method:opts.method||'GET', headers, body:opts.body, signal:ctl.signal, redirect:'manual'}); }
 catch(e){ return JSON.stringify({status:0, ct:'', text:String(e)}); }
 finally { clearTimeout(t); }
 if(r.type==='opaqueredirect') return JSON.stringify({status:302, ct:'', text:'redirect'});
 const out={status:r.status, ct:r.headers.get('content-type'), cd:r.headers.get('content-disposition')};
 if(binary){
   const b=new Uint8Array(await r.arrayBuffer()); let s='';
   for(let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode.apply(null,b.subarray(i,i+0x8000));
   out.b64=btoa(s);
 } else { out.text=await r.text(); }
 return JSON.stringify(out);
})()"""

# The JWT claims of the current session (or null), without fetching any API.
CLAIMS_JS = """(async()=>{
 const r=await fetch(__TOKEN__,{headers:{'X-Requested-With':'XMLHttpRequest'}});
 if(!r.ok || !(r.headers.get('content-type')||'').includes('json')) return null;
 const tok=(await r.json()).cnafTokenJwt;
 return JSON.parse(decodeURIComponent(escape(atob(tok.split(' ')[1].split('.')[1].replace(/-/g,'+').replace(/_/g,'/')))));
})()"""


# ---------------------------------------------------------------------------
# Normalization (pure functions, tested offline)
# ---------------------------------------------------------------------------

def iso_date(yyyymmdd: str | None) -> str | None:
    """'20261105' -> '2026-11-05'; anything else -> None."""
    s = (yyyymmdd or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else None


def iso_datetime(yyyymmdd: str | None, hhmmss: str | None) -> str | None:
    """Date plus time when the API gives one ('000000' means no time)."""
    d = iso_date(yyyymmdd)
    t = (hhmmss or "").strip()
    if d and re.fullmatch(r"\d{6}", t) and t != "000000":
        return f"{d}T{t[:2]}:{t[2:4]}:{t[4:]}"
    return d


def cents(amount: str | None) -> int | None:
    """'1 234,56' -> 123456; '*' or empty (no entitlement) -> None."""
    s = re.sub(r"[\s  ]", "", amount or "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", s):
        return None
    whole, _, frac = s.lstrip("-").partition(".")
    value = int(whole) * 100 + int((frac + "00")[:2])
    return -value if s.startswith("-") else value


def eur(value: int | None) -> str:
    """123456 -> '1 234,56 €' (French formatting, as on caf.fr)."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f",{frac:02d} €"


def yyyymm(value: str) -> str:
    """'2026-03' -> '2026-03'; rejects anything else."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise click.BadParameter(f"{value!r} is not a month; use YYYY-MM, e.g. 2026-03.")
    return value


def month_bounds(start: str, end: str) -> tuple[str, str]:
    """('2026-01', '2026-10') -> ('20260101', '20261031'): the API's period format."""
    y1, m1 = map(int, yyyymm(start).split("-"))
    y2, m2 = map(int, yyyymm(end).split("-"))
    if (y1, m1) > (y2, m2):
        raise click.BadParameter(f"--from {start} is after --to {end}.")
    ny, nm = (y2 + 1, 1) if m2 == 12 else (y2, m2 + 1)
    last = (time.mktime((ny, nm, 1, 12, 0, 0, 0, 0, -1)) - 86400)
    return f"{y1:04d}{m1:02d}01", time.strftime("%Y%m%d", time.localtime(last))


def normalize_payments(raw: dict) -> list[dict]:
    """Payments, latest first; the bank account is masked by the API and dropped here."""
    out = []
    for p in raw.get("paiements") or []:
        out.append({
            "date": iso_date(p.get("dateEmission")),
            "period": p.get("periode"),
            "amount_cents": cents(p.get("montantPaiement")),
            "recovered_cents": cents(p.get("montantRecuperation")),
            "monthly": bool(p.get("paiementMensuel")),
            "recipient": p.get("raisonSociale") or p.get("libelleDestinataire"),
            "recipient_type": p.get("typeDestinataire"),
            "benefits": [{
                "code": b.get("naturePrestation"),
                "label": b.get("libellePrestationCaffr"),
                "amount_cents": cents(b.get("montantPrestation")),
                "note": b.get("libelleRenvoiCaffr"),
            } for b in p.get("prestations") or []],
        })
    return sorted(out, key=lambda p: (p["date"] or "", p["amount_cents"] or 0), reverse=True)


def _benefit(b: dict) -> dict:
    return {
        "code": b.get("codePrestation"),
        "label": b.get("libellePrestationCaffr"),
        "amount_cents": cents(b.get("montant")),
        "suspended": bool(b.get("suspendue")),
        "reason": b.get("libelleResultatCaffr"),
        "note": b.get("libelleRenvoiCaffr"),
    }


def normalize_droits(raw: list) -> list[dict]:
    """Entitlements per month, latest first."""
    out = []
    for m in raw or []:
        out.append({
            "month": (iso_date(m.get("dateDebutPeriode")) or "")[:7] or None,
            "period": m.get("periode"),
            "total_cents": cents(m.get("montantTotal")),
            "suspended_total_cents": cents(m.get("montantTotalSuspendu")),
            "active": [_benefit(b) for b in m.get("prestationsEnCours") or []],
            "suspended": [_benefit(b) for b in m.get("prestationsSuspendues") or []],
            "no_entitlement": [_benefit(b) for b in m.get("prestationsSansDroits") or []],
        })
    return sorted(out, key=lambda m: m["month"] or "", reverse=True)


def message_id(m: dict) -> str:
    """The id `caf courrier`/`caf download` take: clePiece, else a stable hash for own declarations."""
    if m.get("clePiece"):
        return m["clePiece"]
    seed = "|".join(str(m.get(k) or "") for k in ("typeMessage", "dateEnregistrement", "heureEnregistrement",
                                                   "libelleMessageCaffr", "dateTraitement"))
    return "D" + hashlib.sha1(seed.encode()).hexdigest()[:12]


def normalize_messages(raw: dict) -> list[dict]:
    """Letters (courrier), emails (courriel) and own declarations, latest first."""
    out = []
    for m in raw.get("courriersCourriels") or []:
        kind = MESSAGE_KINDS.get(m.get("typeMessage"), m.get("typeMessage"))
        out.append({
            "id": message_id(m),
            "kind": kind,
            "date": iso_datetime(m.get("dateEnregistrement"), m.get("heureEnregistrement")),
            "processed": iso_date(m.get("dateTraitement")),
            "subject": (m.get("libelleMessageCaffr") or "").strip(),
            "status": m.get("libelleEtatTraitementCaffr"),
            "read": bool(m.get("topLu")),
            "pdf": m.get("adresseImage"),
            "text": m.get("texteCourriel"),
            "reply": m.get("texteCourrielReponse"),
            "benefit": m.get("prestation"),
            "claim_reason": m.get("motifReclamation"),
        })
    return sorted(out, key=lambda m: (m["date"] or "", m["id"]), reverse=True)


def message_errors(raw: dict) -> list[str]:
    """Sections the API says it failed to load (it then answers 200 with them missing)."""
    names = {"courriersEnErreur": "letters", "courrielsEnErreur": "emails", "relevesEnErreur": "statements"}
    return [label for key, label in names.items() if raw.get(key)]


def normalize_demarches(raw: dict) -> list[dict]:
    """Online procedures (déclarations, demandes), latest first; `id` is the receipt PDF."""
    out = []
    for status, key in (("to_complete", "listeDemarchesACompleter"), ("done", "listeDemarchesCompletes")):
        for d in raw.get(key) or []:
            out.append({
                "id": d.get("adresseImage"),
                "date": iso_datetime(d.get("dateDemarche"), d.get("heureDemarche")),
                "status": status,
                "processed": bool(d.get("traite")),
                "requests": [{
                    "nature": r.get("natureDemandeCaffr"),
                    "state": r.get("etatDemandeCaffr"),
                    "state_date": iso_date(r.get("dateEtat")),
                } for r in d.get("demandesList") or []],
            })
    return sorted(out, key=lambda d: (d["date"] or "", d["id"] or ""), reverse=True)


def normalize_profile(raw: dict, summary: dict) -> dict:
    """Household profile. Social security numbers (NIR) are never output."""
    def person(p: dict) -> dict:
        return {
            "role": p.get("type"),
            "first_name": p.get("prenom"),
            "last_name": p.get("nom"),
            "birth_date": (p.get("dateNaissance") or "")[:10] or None,
            "activity": (p.get("sitpro") or {}).get("libAct"),
            "activity_since": (p.get("sitpro") or {}).get("libDateDebAct"),
        }
    user = raw.get("utilisateur") or {}
    lines = [summary.get(k) for k in ("libSituationFamille", "libPersonnesACharge", "libAutrePersonne",
                                      "libActivite", "libConjointActivite")]
    address = [summary.get(f"ligne{i}Adresse") for i in range(2, 8)]
    return {
        "holder": person(user),
        "household": [person(p) for p in raw.get("personnes") or []],
        "family_situation": (raw.get("sitfam") or {}).get("libSitFam"),
        "summary": [s for s in lines if s],
        "address": [a for a in address if a],
        "email": (user.get("coordonneesContact") or {}).get("mail"),
    }


def pdf_key(address: str) -> str:
    """read_pdf takes the image address without its 3-char CAF prefix and 'PDF' suffix."""
    if not re.fullmatch(r"\d{3}[A-Z0-9]+PDF", address or ""):
        raise click.BadParameter(f"{address!r} is not a CAF document address (e.g. 751T0000000000000000000PDF).")
    return address[3:-3]


def safe_name(text: str) -> str:
    """A file name valid on macOS, Windows and Drive, in NFC form."""
    s = unicodedata.normalize("NFC", text or "")
    s = re.sub(r'[\\/:*?"<>|\0]', "-", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:150] or "document"


def doc_name(label: str, address: str) -> str:
    """'<label> <address>.pdf', the label shortened so the unique address always survives."""
    return safe_name(f"{safe_name(label)[:120]} {address}".strip()) + ".pdf"


def build_email(topic: str, body: str, nom_caf: str, motif: str | None = None,
                prestation: str | None = None) -> dict:
    """The JSON the signed-in contact form posts to envoi_mail."""
    code = topic.upper()
    if code not in TOPICS:
        raise click.BadParameter(f"unknown topic {topic!r}; list them with `caf topics`.")
    if code == "INFO_REC":
        if not motif or motif.upper() not in RECLAMATION_MOTIFS:
            raise click.BadParameter("a réclamation needs --motif RECL_…; list them with `caf topics`.")
        if RECLAMATION_MOTIFS[motif.upper()][1] == "dossier" and (not prestation or prestation.upper() not in PRESTATIONS):
            raise click.BadParameter(f"motif {motif.upper()} needs --prestation PRES_…; list them with `caf topics`.")
    elif motif or prestation:
        raise click.BadParameter("--motif and --prestation only go with --topic INFO_REC.")
    msg = CONTROL_CHARS.sub("", body.replace("\r\n", "\n")).strip()
    if not msg:
        raise click.BadParameter("empty message; pass --body TEXT or --body-file FILE.")
    if len(msg) > MAX_MESSAGE_CHARS:
        raise click.BadParameter(f"message is {len(msg)} characters; the CAF form takes at most {MAX_MESSAGE_CHARS}.")
    is_rec = code == "INFO_REC"
    pres = prestation.upper() if is_rec and prestation and RECLAMATION_MOTIFS[motif.upper()][1] == "dossier" else None
    return {
        "objetMessage": code,
        "motifReclamation": motif.upper() if is_rec else None,
        "prestation": pres,
        "msg": msg,
        "nomCaf": nom_caf,
        "idSousMotifMessage": TOPICS[code][1],
    }


# ---------------------------------------------------------------------------
# CDP plumbing
# ---------------------------------------------------------------------------

def _http_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _targets_or_none(cdp_url: str) -> list[dict] | None:
    try:
        return _http_json(cdp_url.rstrip("/") + "/json")
    except Exception:  # noqa: BLE001 — any failure means "no Chrome there"
        return None


def _chrome_binary() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return None


def _launch_chrome(cdp_url: str, url: str) -> None:
    port = urllib.parse.urlparse(cdp_url).port or 9225
    binary = _chrome_binary()
    if not binary:
        raise click.ClickException(
            "Chrome not found; install Google Chrome or start one with "
            f"--remote-debugging-port={port} --user-data-dir={PROFILE_DIR}")
    log.info("starting the dedicated Chrome (profile %s, port %s)", PROFILE_DIR, port)
    subprocess.Popen(
        [binary, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _targets(cdp_url: str, auto_launch: bool) -> list[dict]:
    targets = _targets_or_none(cdp_url)
    if targets is not None:
        return targets
    host = urllib.parse.urlparse(cdp_url).hostname
    if not auto_launch or host not in ("localhost", "127.0.0.1", "::1"):
        raise click.ClickException(f"No Chrome DevTools at {cdp_url}; drop --no-launch or start Chrome.")
    _launch_chrome(cdp_url, HOME_URL)
    for _ in range(40):
        time.sleep(0.5)
        if (targets := _targets_or_none(cdp_url)) is not None:
            return targets
    raise click.ClickException("Chrome started but its DevTools port never opened.")


class Tab:
    """One Chrome tab driven over its CDP websocket."""

    def __init__(self, ws_url: str):
        # Chrome rejects the handshake when an Origin header is present.
        self.ws = websocket.create_connection(ws_url, timeout=180, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise click.ClickException(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def ev(self, expression: str):
        res = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"].get("exception", {}).get("description") or res["exceptionDetails"].get("text")
            raise click.ClickException(f"page error: {str(detail)[:300]}")
        return res.get("result", {}).get("value")

    def href(self) -> str:
        try:
            return self.ev("location.href") or ""
        except click.ClickException:
            return ""  # context swapped mid-navigation

    def navigate(self, url: str, timeout: float = 30) -> str:
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        time.sleep(0.5)
        while time.time() < deadline:
            try:
                if self.ev("document.readyState") == "complete":
                    break
            except click.ClickException:
                pass  # context swapped mid-navigation
            time.sleep(0.3)
        return self.href()


def open_tab(cdp_url: str, auto_launch: bool) -> Tab:
    """Attach to a caf.fr tab of the dedicated Chrome, opening one if needed."""
    pages = [t for t in _targets(cdp_url, auto_launch) if t.get("type") == "page"]
    mine = [t for t in pages if "caf.fr" in (t.get("url") or "")]
    # Prefer a tab already on the portal: its fetches carry the session as is.
    mine.sort(key=lambda t: not (t.get("url") or "").startswith(ORIGIN))
    if mine:
        target = mine[0]
    else:
        target = _http_json(cdp_url.rstrip("/") + "/json/new?" + urllib.parse.quote(HOME_URL, safe=""), "PUT")
        time.sleep(1)
    return Tab(target["webSocketDebuggerUrl"])


class Caf:
    """The Mon Compte API, called from inside a signed-in wwwd.caf.fr tab."""

    def __init__(self, tab: Tab):
        self.tab = tab

    def ensure_on_portal(self) -> None:
        """In-page fetches need the wwwd.caf.fr origin; a login page means signed out."""
        href = self.tab.href()
        if not href.startswith(ORIGIN):
            href = self.tab.navigate(HOME_URL)
        if not href.startswith(ORIGIN):
            raise click.ClickException(NOT_LOGGED)

    def claims(self) -> dict | None:
        self.ensure_on_portal()
        return self.tab.ev(CLAIMS_JS.replace("__TOKEN__", json.dumps(TOKEN_PATH)))

    def request(self, path: str, method: str = "GET", body: dict | None = None, binary: bool = False) -> dict:
        """One API call with bounded retries on 429/5xx."""
        self.ensure_on_portal()
        opts: dict = {"method": method}
        if body is not None:
            opts["body"] = json.dumps(body, ensure_ascii=False)
            opts["headers"] = {"Content-Type": "application/json;charset=UTF-8"}
        js = (FETCH_JS.replace("__PATH__", json.dumps(path)).replace("__OPTS__", json.dumps(opts))
              .replace("__BINARY__", json.dumps(binary)).replace("__TOKEN__", json.dumps(TOKEN_PATH))
              .replace("__TIMEOUT__", str(FETCH_TIMEOUT_MS)))
        for attempt in range(MAX_RETRIES + 1):
            res = json.loads(self.tab.ev(js))
            status = res["status"]
            if status != 429 and status < 500 and status != 0 or attempt == MAX_RETRIES:
                break
            delay = 2 ** attempt
            log.warning("%s %s → %s, retrying in %ds", method, path, status or "network error", delay)
            time.sleep(delay)
        if status in (401, 302, 303):
            raise click.ClickException(f"{status or 'redirect'} — caf.fr session expired. {NOT_LOGGED}")
        if status == 403:
            raise click.ClickException(f"403 — this account may not read {path}.")
        if status == 404:
            raise click.ClickException(f"404 — {path} not found on caf.fr.")
        if status == 409 and "/attestation_sur_periode/" in path:
            raise click.ClickException("409 — caf.fr has no attestation for this period yet; "
                                       "end it at the last closed month (usually the previous one).")
        if not 200 <= status < 300:
            raise click.ClickException(f"{status} — {method} {path} failed: {(res.get('text') or '')[:200]}")
        return res

    def json(self, path: str, method: str = "GET", body: dict | None = None):
        res = self.request(path, method, body)
        text = res.get("text") or ""
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise click.ClickException(f"{path} did not answer JSON (session expired?): {text[:120]!r}") from exc

    def pdf(self, path: str) -> bytes:
        data = base64.b64decode(self.request(path, binary=True)["b64"])
        if not data.startswith(b"%PDF"):
            raise click.ClickException(f"{path} did not return a PDF ({len(data)} bytes).")
        return data


def client(ctx: click.Context) -> Caf:
    o = ctx.obj
    return Caf(open_tab(o["cdp_url"], o["auto_launch"]))


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def emit(data, as_json: bool, render) -> None:
    if as_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)


def write_pdf(dest: Path, data: bytes) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return str(dest.resolve())


JSON_OPT = click.option("--json", "as_json", is_flag=True, help="Normalized JSON output.")
RAW_OPT = click.option("--raw", is_flag=True, help="The API's answer, untouched (debugging).")
LIMIT_OPT = click.option("--limit", type=int, default=None, help="At most N rows.")
OUT_DIR_OPT = click.option("--target-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
                           help="Folder for the PDF(s) [default: $CAF_DOWNLOAD_DIR or ~/Downloads/caf].")
OUT_FILE_OPT = click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path),
                            help="Exact output file path.")


def raw_out(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]},
             epilog="\b\nExamples:\n  caf connect\n  caf paiements --limit 5\n  caf courriers --kind courrier\n"
                    "  caf download 751T0000000000000000000PDF\n  caf attestation qf")
@click.option("--cdp-url", default=DEFAULT_CDP, show_default=True, help="DevTools URL of the dedicated Chrome.")
@click.option("--launch/--no-launch", "auto_launch", default=True, show_default=True,
              help="Start the dedicated Chrome when it is not running.")
@click.option("--debug", is_flag=True, help="Log debug details to stderr.")
@click.pass_context
def cli(ctx: click.Context, cdp_url: str, auto_launch: bool, debug: bool) -> None:
    """caf.fr Mon Compte: payments, droits, letters, attestations, emails to the CAF."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="%(message)s", stream=sys.stderr)
    ctx.obj = {"cdp_url": cdp_url, "auto_launch": auto_launch}


@cli.command(epilog="\b\nExamples:\n  caf login\n  caf login --timeout 600")
@click.option("--timeout", default=300, show_default=True, help="Seconds to wait for the sign-in.")
@click.pass_context
def login(ctx: click.Context, timeout: int) -> None:
    """Open the dedicated Chrome on Mon Compte and wait for the sign-in."""
    tab = open_tab(ctx.obj["cdp_url"], ctx.obj["auto_launch"])
    if not tab.href().startswith(ORIGIN):
        tab.navigate(HOME_URL)
    tab.call("Page.bringToFront")
    log.info("Sign in in the Chrome window (numéro allocataire + mot de passe, or FranceConnect)…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tab.href().startswith(ORIGIN):
            try:
                if Caf(tab).claims():
                    click.echo("signed in")
                    return
            except click.ClickException:
                pass
        time.sleep(2)
    raise click.ClickException(f"not signed in after {timeout} s; re-run `caf login`.")


@cli.command(epilog="\b\nExamples:\n  caf connect\n  caf connect --json")
@JSON_OPT
@click.pass_context
def connect(ctx: click.Context, as_json: bool) -> None:
    """Open the dedicated Chrome on caf.fr and report whether it is signed in (returns at once).

    When it is not, the window shows the CAF login page: sign in there
    (numéro allocataire + mot de passe, or FranceConnect), then run any command.
    """
    tab = open_tab(ctx.obj["cdp_url"], ctx.obj["auto_launch"])
    claims = None
    if tab.href().startswith(ORIGIN):
        try:
            claims = Caf(tab).claims()
        except click.ClickException:
            claims = None
    if not claims:
        tab.navigate(HOME_URL)
        tab.call("Page.bringToFront")
    data = {
        "connected": bool(claims),
        "caf": (claims or {}).get("codeOrga"),
        "dossier": (claims or {}).get("matricule"),
        "next": None if claims else "Sign in in the Chrome window that just opened on the CAF login page "
                                     "(numéro allocataire + mot de passe, or FranceConnect), then ask again.",
    }
    emit(data, as_json, lambda d: click.echo(
        f"connected\tCAF {d['caf']}\tdossier {d['dossier']}" if d["connected"] else f"not signed in\t{d['next']}"))


@cli.command(epilog="\b\nExamples:\n  caf whoami\n  caf whoami --json")
@JSON_OPT
@click.pass_context
def whoami(ctx: click.Context, as_json: bool) -> None:
    """The signed-in allocataire: name, CAF, dossier number, unread messages."""
    caf = client(ctx)
    claims = caf.claims()
    if not claims:
        raise click.ClickException(NOT_LOGGED)
    profile = caf.json(API["profil"]) or {}
    user = profile.get("utilisateur") or {}
    unread = (caf.json(API["non_lus"]) or {}).get("messagesNonLus")
    data = {
        "name": " ".join(x for x in (user.get("prenom"), user.get("nom")) if x) or None,
        "caf": claims.get("codeOrga"),
        "dossier": claims.get("matricule"),
        "france_connect": bool(claims.get("typeFranceConnect")),
        "unread_messages": bool(unread),
        "token_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(claims.get("exp", 0))),
    }
    emit(data, as_json, lambda d: click.echo(
        f"{d['name']}\tCAF {d['caf']}\tdossier {d['dossier']}\tunread={'yes' if d['unread_messages'] else 'no'}"))


@cli.command(epilog="\b\nExamples:\n  caf profil\n  caf profil --json")
@JSON_OPT
@RAW_OPT
@click.pass_context
def profil(ctx: click.Context, as_json: bool, raw: bool) -> None:
    """Household: holder, partner, children, situation and address (no NIR)."""
    caf = client(ctx)
    full, summary = caf.json(API["profil"]) or {}, caf.json(API["profil_reduit"]) or {}
    if raw:
        raw_out({"profilcalp": full, "profil_reduit": summary})
        return
    data = normalize_profile(full, summary)

    def render(d):
        for p in [d["holder"], *d["household"]]:
            click.echo(f"{p['role']}\t{p['first_name']} {p['last_name']}\t{p['birth_date'] or ''}\t{p['activity'] or ''}")
        for line in d["summary"]:
            click.echo(line)
        click.echo("Adresse: " + ", ".join(d["address"]))
    emit(data, as_json, render)


@cli.command(epilog="\b\nExamples:\n  caf paiements\n  caf paiements --since 2026-01 --json\n  caf paiements --limit 3")
@click.option("--since", metavar="YYYY-MM", help="Payments sent on or after this month.")
@LIMIT_OPT
@JSON_OPT
@RAW_OPT
@click.pass_context
def paiements(ctx: click.Context, since: str | None, limit: int | None, as_json: bool, raw: bool) -> None:
    """Payments the CAF sent, latest first, with the benefits each one covers."""
    data = client(ctx).json(API["paiements"]) or {}
    if raw:
        raw_out(data)
        return
    rows = normalize_payments(data)
    if since:
        rows = [r for r in rows if (r["date"] or "") >= yyyymm(since)]
    rows = rows[:limit]

    def render(rs):
        for r in rs:
            what = ", ".join(f"{b['label']} {eur(b['amount_cents'])}" for b in r["benefits"])
            click.echo(f"{r['date']}\t{eur(r['amount_cents'])}\t{r['period']}\t{what}")
    emit(rows, as_json, render)


@cli.command(epilog="\b\nExamples:\n  caf droits\n  caf droits --months 6\n  caf droits --month 2026-03 --json")
@click.option("--month", "month", metavar="YYYY-MM", help="Only this month.")
@click.option("--months", type=int, default=1, show_default=True, help="The latest N months (the API keeps 24).")
@click.option("--no-entitlement", "show_none", is_flag=True, help="Also list benefits with no entitlement and why.")
@JSON_OPT
@RAW_OPT
@click.pass_context
def droits(ctx: click.Context, month: str | None, months: int, show_none: bool, as_json: bool, raw: bool) -> None:
    """Monthly entitlements: benefits in payment, suspended, and refused with the reason."""
    data = client(ctx).json(API["droits"]) or []
    if raw:
        raw_out(data)
        return
    rows = normalize_droits(data)
    if month:
        rows = [r for r in rows if r["month"] == yyyymm(month)]
        if not rows:
            raise click.ClickException(f"no droits for {month}; the API keeps the last 24 months.")
    else:
        rows = rows[:months]

    def render(rs):
        for r in rs:
            click.echo(f"{r['month']}\ttotal {eur(r['total_cents'])}")
            for b in r["active"]:
                click.echo(f"  en cours\t{eur(b['amount_cents'])}\t{b['label']}\t{b['code']}")
            for b in r["suspended"]:
                click.echo(f"  suspendu\t{eur(b['amount_cents'])}\t{b['label']}\t{b['code']}")
            if show_none:
                for b in r["no_entitlement"]:
                    click.echo(f"  sans droit\t{b['label']}\t{b['reason'] or ''}\t{b['code']}")
    emit(rows, as_json, render)


def fetch_messages(caf: Caf) -> list[dict]:
    raw = caf.json(API["courriers"]) or {}
    if failed := message_errors(raw):
        log.warning("caf.fr could not load its %s; the list may be incomplete.", ", ".join(failed))
    return normalize_messages(raw)


@cli.command(epilog="\b\nExamples:\n  caf courriers\n  caf courriers --kind courrier --limit 5\n"
                    "  caf courriers --kind courriel --json\n  caf courriers --unread")
@click.option("--kind", type=click.Choice(["courrier", "courriel", "declaration"]), multiple=True,
              help="courrier = letter from the CAF (PDF), courriel = email you sent and its status, "
                   "declaration = acknowledgement of a declaration. Repeatable.")
@click.option("--unread", is_flag=True, help="Only items not opened yet.")
@click.option("--since", metavar="YYYY-MM", help="Received on or after this month.")
@LIMIT_OPT
@JSON_OPT
@RAW_OPT
@click.pass_context
def courriers(ctx, kind, unread, since, limit, as_json, raw) -> None:
    """Letters and emails of « Mes courriers, courriels », latest first (id first)."""
    caf = client(ctx)
    if raw:
        raw_out(caf.json(API["courriers"]))
        return
    rows = fetch_messages(caf)
    if kind:
        rows = [r for r in rows if r["kind"] in kind]
    if unread:
        rows = [r for r in rows if not r["read"]]
    if since:
        rows = [r for r in rows if (r["date"] or "") >= yyyymm(since)]
    rows = rows[:limit]
    if as_json:
        emit(rows, True, None)
        return
    for r in rows:
        flag = "" if r["read"] else "*"
        status = r["status"] or ("PDF" if r["pdf"] else "")
        click.echo(f"{r['id']}\t{(r['date'] or '')[:10]}\t{r['kind']}{flag}\t{r['subject']}\t{status}")


@cli.command(epilog="\b\nExamples:\n  caf courrier A751000000000A0000000A\n"
                    "  caf courrier A751000000000A0000000A A751000000000B0000000B --json\n"
                    "  caf courrier A751000000000A0000000A --save")
@click.argument("ids", nargs=-1, required=True)
@click.option("--save", is_flag=True, help="Also download the letter's PDF.")
@OUT_DIR_OPT
@JSON_OPT
@click.pass_context
def courrier(ctx, ids, save, target_dir, as_json) -> None:
    """Show letters/emails by id: an email's full text and status, a letter's PDF address."""
    caf = client(ctx)
    by_id = {m["id"]: m for m in fetch_messages(caf)}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise click.ClickException(f"unknown id(s) {missing}; list them with `caf courriers`.")
    rows = [dict(by_id[i]) for i in ids]
    if save:
        folder = target_dir or default_out()
        for r in rows:
            if r["pdf"]:
                name = doc_name(f"{(r['date'] or '')[:10]} {r['subject']}", r["pdf"])
                r["path"] = write_pdf(folder / name, caf.pdf(API["pdf"] + pdf_key(r["pdf"])))

    def render(rs):
        for r in rs:
            click.echo(f"# {r['subject']}\t{r['kind']}\t{r['date']}\t{r['id']}")
            if r["status"]:
                click.echo(f"Statut: {r['status']}")
            if r["text"]:
                click.echo(r["text"].strip())
            if r["reply"]:
                click.echo(f"\nRéponse de la Caf: {r['reply'].strip()}")
            if r["pdf"]:
                click.echo(r.get("path") or f"PDF: `caf download {r['pdf']}`")
            click.echo("")
    emit(rows, as_json, render)


@cli.command(epilog="\b\nExamples:\n  caf demarches\n  caf demarches --limit 3 --json")
@LIMIT_OPT
@JSON_OPT
@RAW_OPT
@click.pass_context
def demarches(ctx, limit, as_json, raw) -> None:
    """Online procedures (déclarations, demandes) and their state; id = receipt PDF."""
    data = client(ctx).json(API["demarches"]) or {}
    if raw:
        raw_out(data)
        return
    rows = normalize_demarches(data)[:limit]

    def render(rs):
        for r in rs:
            what = " + ".join(q["nature"] or "" for q in r["requests"])
            state = r["requests"][0]["state"] if r["requests"] else ""
            click.echo(f"{r['id']}\t{(r['date'] or '')[:10]}\t{r['status']}\t{what}\t{state}")
    emit(rows, as_json, render)


@cli.command(epilog="\b\nExamples:\n  caf download 751T0000000000000000000PDF\n"
                    "  caf download A751000000000A0000000A 751O0000000000000000000PDF --target-dir ~/CAF\n"
                    "  caf download 751T0000000000000000000PDF -o lettre.pdf --json")
@click.argument("ids", nargs=-1, required=True)
@OUT_DIR_OPT
@OUT_FILE_OPT
@JSON_OPT
@click.pass_context
def download(ctx, ids, target_dir, output, as_json) -> None:
    """Download letters (courrier id or PDF address) and procedure receipts (démarche id) as PDF."""
    if output and len(ids) != 1:
        raise click.BadParameter("-o takes exactly one id, e.g. `caf download 751T…PDF -o lettre.pdf`.")
    caf = client(ctx)
    # Names come from the listings; a bare PDF address absent from them still downloads.
    names: dict[str, str] = {}
    by_message: dict[str, str] = {}
    for m in fetch_messages(caf):
        if m["pdf"]:
            names[m["pdf"]] = f"{(m['date'] or '')[:10]} {m['subject']}"
            by_message[m["id"]] = m["pdf"]
    for d in normalize_demarches(caf.json(API["demarches"]) or {}):
        if d["id"]:
            names[d["id"]] = f"{(d['date'] or '')[:10]} " + " + ".join(q["nature"] or "" for q in d["requests"])
    out = []
    for i in ids:
        address = by_message.get(i, i)
        if i.startswith("A") and i not in by_message:
            raise click.ClickException(f"{i} has no PDF (an email or a declaration); see `caf courrier {i}`.")
        dest = output or (target_dir or default_out()) / doc_name(names.get(address, ""), address)
        out.append({"id": i, "path": write_pdf(dest, caf.pdf(API["pdf"] + pdf_key(address)))})
    emit(out, as_json, lambda rs: [click.echo(f"{r['id']}\t{r['path']}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  caf attestation paiements\n  caf attestation qf -o qf.pdf\n"
                    "  caf attestation paiements --from 2026-01 --to 2026-10 --json\n  caf attestation net-social")
@click.argument("kind", type=click.Choice(list(ATTESTATIONS)))
@click.option("--from", "start", metavar="YYYY-MM", help="First month of the period (with --to).")
@click.option("--to", "end", metavar="YYYY-MM", help="Last month of the period (with --from).")
@OUT_DIR_OPT
@OUT_FILE_OPT
@JSON_OPT
@click.pass_context
def attestation(ctx, kind, start, end, target_dir, output, as_json) -> None:
    """Download an attestation PDF: paiements, qf (quotient familial) or net-social.

    Without --from/--to, the latest one (as on caf.fr); with both, the period.
    """
    suffix, label = ATTESTATIONS[kind]
    if bool(start) != bool(end):
        raise click.BadParameter("pass both --from and --to, e.g. --from 2026-01 --to 2026-10.")
    if start:
        d1, d2 = month_bounds(start, end)
        path, name = f"{API['attestation_period']}{suffix}/{d1}/{d2}", f"{label} {start} à {end}"
    else:
        path, name = API["attestation_last"] + suffix, f"{label} {time.strftime('%Y-%m-%d')}"
    dest = output or (target_dir or default_out()) / (safe_name(name) + ".pdf")
    data = {"kind": kind, "path": write_pdf(dest, client(ctx).pdf(path))}
    emit(data, as_json, lambda d: click.echo(d["path"]))


@cli.command(epilog="\b\nExamples:\n  caf releve\n  caf releve -o releve.pdf --json")
@OUT_DIR_OPT
@OUT_FILE_OPT
@JSON_OPT
@click.pass_context
def releve(ctx, target_dir, output, as_json) -> None:
    """Download the latest monthly account statement (relevé de compte) PDF."""
    dest = output or (target_dir or default_out()) / f"Relevé de compte {time.strftime('%Y-%m-%d')}.pdf"
    data = {"path": write_pdf(dest, client(ctx).pdf(API["releve"]))}
    emit(data, as_json, lambda d: click.echo(d["path"]))


@cli.command(epilog="\b\nExamples:\n  caf topics\n  caf topics --json")
@JSON_OPT
def topics(as_json) -> None:
    """Email subjects for `caf send`, plus the réclamation motifs and prestations."""
    data = {
        "topics": [{"code": k, "label": v[0]} for k, v in TOPICS.items()],
        "reclamation_motifs": [{"code": k, "label": v[0], "needs_prestation": v[1] == "dossier"}
                               for k, v in RECLAMATION_MOTIFS.items()],
        "prestations": [{"code": k, "label": v} for k, v in PRESTATIONS.items()],
    }

    def render(d):
        for t in d["topics"]:
            click.echo(f"{t['code']}\t{t['label']}")
        click.echo("\n# --motif (with --topic INFO_REC)")
        for m in d["reclamation_motifs"]:
            click.echo(f"{m['code']}\t{m['label']}" + ("\t(needs --prestation)" if m["needs_prestation"] else ""))
        click.echo("\n# --prestation")
        for p in d["prestations"]:
            click.echo(f"{p['code']}\t{p['label']}")
    emit(data, as_json, render)


@cli.command(epilog="\b\nExamples:\n  caf send --topic INFO_DOS --body-file question.txt\n"
                    "  caf send --topic INFO_DOS --body-file question.txt --yes\n"
                    "  caf send --topic INFO_REC --motif RECL_PAI --prestation PRES_PAJ --body 'Bonjour, …' --yes")
@click.option("--topic", required=True, help="Subject code from `caf topics`, e.g. INFO_DOS.")
@click.option("--body", help="Message text (at most 3000 characters).")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Read the message from a file.")
@click.option("--motif", help="Réclamation motif (RECL_…), only with --topic INFO_REC.")
@click.option("--prestation", help="Benefit concerned (PRES_…), for the réclamation motifs about your file.")
@click.option("--yes", is_flag=True, help="Really send. Without it: check everything and print the request.")
@JSON_OPT
@click.pass_context
def send(ctx, topic, body, body_file, motif, prestation, yes, as_json) -> None:
    """Email your CAF through « Contacter ma Caf » (dry run unless --yes).

    The CAF answers by email or letter; the message then shows in `caf courriers --kind courriel`.
    """
    if bool(body) == bool(body_file):
        raise click.BadParameter("pass exactly one of --body TEXT or --body-file FILE.")
    text = body if body else body_file.read_text(encoding="utf-8")
    caf = client(ctx)
    if not (caf.json(API["courriel_autorise"]) or {}).get("courrielAutorise"):
        raise click.ClickException("caf.fr does not allow this account to email its CAF; call 3230 or book a visit.")
    if not (caf.json(API["coordonnees"]) or {}).get("adresseEmail"):
        raise click.ClickException("no email address on the CAF file; add one on caf.fr (Mon profil) first.")
    nom_caf = (caf.json(API["pages_locales"]) or {}).get("caf_nom")
    if not nom_caf:
        raise click.ClickException("caf.fr did not say which CAF handles this file (pages_locales has no caf_nom).")
    payload = build_email(topic, text, nom_caf, motif, prestation)
    if not yes:
        emit({"sent": False, "dry_run": True, "endpoint": API["envoi_mail"], "payload": payload}, as_json,
             lambda d: click.echo(f"DRY RUN — would POST {d['endpoint']}\n"
                                  + json.dumps(d["payload"], ensure_ascii=False, indent=2)
                                  + "\nRe-run with --yes to send."))
        return
    answer = caf.json(API["envoi_mail"], method="POST", body=payload)
    data = {"sent": True, "topic": payload["objetMessage"], "answer": answer}
    emit(data, as_json, lambda d: click.echo(f"sent\t{d['topic']}"))


if __name__ == "__main__":
    cli()
