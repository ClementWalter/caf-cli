#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "websocket-client==1.9.2",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Offline tests for caf-cli: normalization, email payload checks, CLI wiring.

Fixtures are synthetic records shaped like the caf.fr API answers (same keys),
with made-up names, amounts and ids. The browser is replaced by a stub `Caf`
that serves those records by API path.
"""

import json
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import caf_cli as cc  # noqa: E402

PAIEMENTS = {"paiements": [
    {"dateEmission": "20261005", "periode": "Septembre 2026", "paiementMensuel": True,
     "montantPaiement": "38,06", "montantRecuperation": "0,00", "typeDestinataire": "allocataire",
     "raisonSociale": "", "libelleDestinataire": "DUPONT",
     "compteVersement": "XXXX XXXX 0000 BIC : TESTFRPPXXX",
     "prestations": [{"montantPrestation": "38,06", "naturePrestation": "AFR",
                      "libellePrestationCaffr": "Allocations familiales", "libelleRenvoiCaffr": None}]},
    {"dateEmission": "20261106", "periode": "Octobre 2026", "paiementMensuel": False,
     "montantPaiement": "1 328,15", "montantRecuperation": "0,00", "typeDestinataire": "allocataire",
     "raisonSociale": "", "libelleDestinataire": "DUPONT", "compteVersement": "XXXX",
     "prestations": [{"montantPrestation": "1 328,15", "naturePrestation": "PJ5",
                      "libellePrestationCaffr": "Complément mode de garde", "libelleRenvoiCaffr": None}]},
]}

DROITS = [
    {"periode": "Octobre 2026", "dateDebutPeriode": "20261001", "montantTotal": "38,06",
     "montantTotalSuspendu": "0,00", "prestationsEnCours": [], "prestationsSuspendues": [],
     "prestationsSansDroits": []},
    {"periode": "Novembre 2026", "dateDebutPeriode": "20261101", "montantTotal": "38,06",
     "montantTotalSuspendu": "0,00",
     "prestationsEnCours": [{"montant": "38,06", "codePrestation": "AFRN1", "suspendue": False,
                             "libellePrestationCaffr": "Allocations familiales"}],
     "prestationsSuspendues": [],
     "prestationsSansDroits": [{"montant": "*", "codePrestation": "PJ2N1", "suspendue": False,
                                "libellePrestationCaffr": "Allocation de base - Paje",
                                "libelleResultatCaffr": "Ressources supérieures au plafond"}]},
]

COURRIERS = {"courriersEnErreur": False, "courrielsEnErreur": True, "relevesEnErreur": False,
             "courriersCourriels": [
    {"typeMessage": "courrieralloc", "dateEnregistrement": "20261102", "heureEnregistrement": "000000",
     "dateTraitement": "20261113", "libelleMessageCaffr": "Nous avons reçu les informations ",
     "libelleEtatTraitementCaffr": "Votre dossier a été traité", "topLu": True, "clePiece": None,
     "adresseImage": None, "texteCourriel": None, "texteCourrielReponse": None},
    {"typeMessage": "courriercaf", "dateEnregistrement": "20261026", "heureEnregistrement": "000000",
     "dateTraitement": None, "libelleMessageCaffr": "Courrier de votre Caf", "topLu": False,
     "libelleEtatTraitementCaffr": None, "clePiece": "A751000000000A0000000A",
     "adresseImage": "751T0000000000000000000PDF", "texteCourriel": None, "texteCourrielReponse": None},
    {"typeMessage": "mailcaf", "dateEnregistrement": "20260213", "heureEnregistrement": "132042",
     "dateTraitement": "20260319", "libelleMessageCaffr": "Suivi de votre dossier", "topLu": True,
     "libelleEtatTraitementCaffr": None, "clePiece": "A751000000000B0000000B", "adresseImage": None,
     "texteCourriel": "Bonjour, une question.", "texteCourrielReponse": "Votre courriel a été traité."},
]}

DEMARCHES = {"listeDemarchesACompleter": [], "listeDemarchesCompletes": [
    {"dateDemarche": "20261011", "heureDemarche": "145708", "adresseImage": "751O0000000000000000000PDF",
     "traite": True, "demandesList": [{"natureDemandeCaffr": "Déclaration de situation",
                                       "etatDemandeCaffr": "Nous avons traité votre déclaration.",
                                       "dateEtat": "20261011"}]},
]}

PROFIL = {"utilisateur": {"type": "RESP", "nom": "DUPONT", "prenom": "JEAN", "nir": "100000000000000",
                          "dateNaissance": "1980-01-01T00:00:00.000+00:00",
                          "coordonneesContact": {"mail": "j***@e***.com"},
                          "sitpro": {"libAct": "Activité salariée", "libDateDebAct": "1 janvier 2020"}},
          "personnes": [{"type": "ENF", "nom": "DUPONT", "prenom": "LOU", "nir": "200000000000000",
                         "dateNaissance": "2020-01-01T00:00:00.000+00:00", "sitpro": None}],
          "sitfam": {"libSitFam": "marié"}}
PROFIL_REDUIT = {"libSituationFamille": "Je suis marié.", "ligne4Adresse": "1 RUE DE L'EXEMPLE",
                 "ligne6Adresse": "00000 VILLE", "ligne7Adresse": "FRANCE"}

SERVED = {
    cc.API["paiements"]: PAIEMENTS,
    cc.API["droits"]: DROITS,
    cc.API["courriers"]: COURRIERS,
    cc.API["demarches"]: DEMARCHES,
    cc.API["profil"]: PROFIL,
    cc.API["profil_reduit"]: PROFIL_REDUIT,
    cc.API["non_lus"]: {"messagesNonLus": True},
    cc.API["courriel_autorise"]: {"courrielAutorise": True},
    cc.API["coordonnees"]: {"adresseEmail": "someone@example.com"},
    cc.API["pages_locales"]: {"caf_nom": "Caisse d'Allocations familiales de Test"},
}


class StubCaf:
    """Serves the fixtures by path, like `Caf` serves the live API; records POSTs and PDF paths."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.pdfs: list[str] = []

    def claims(self):
        return {"codeOrga": "000", "matricule": "1234567", "exp": 0, "typeFranceConnect": "fc"}

    def json(self, path, method="GET", body=None):
        if method == "POST":
            self.posts.append((path, body))
            return {"codeRetour": 0}
        return SERVED[path]

    def pdf(self, path):
        self.pdfs.append(path)
        return b"%PDF-1.7 stub"


@pytest.fixture
def stub(monkeypatch):
    s = StubCaf()
    monkeypatch.setattr(cc, "client", lambda ctx: s)
    return s


@pytest.fixture
def run(stub):
    def _run(*args):
        return CliRunner().invoke(cc.cli, list(args))
    return _run


# --- pure helpers ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("38,06", 3806), ("1 328,15", 132815), ("1 328,15", 132815), ("0,00", 0),
    ("12", 1200), ("-5,5", -550), ("*", None), ("", None), (None, None),
])
def test_cents(raw, expected):
    assert cc.cents(raw) == expected


@pytest.mark.parametrize("value,expected", [
    (3806, "38,06 €"), (132815, "1 328,15 €"), (-550, "-5,50 €"), (None, "-"),
])
def test_eur(value, expected):
    assert cc.eur(value) == expected


@pytest.mark.parametrize("raw,expected", [("20261105", "2026-11-05"), ("2026110", None), (None, None)])
def test_iso_date(raw, expected):
    assert cc.iso_date(raw) == expected


@pytest.mark.parametrize("d,t,expected", [
    ("20260213", "132042", "2026-02-13T13:20:42"), ("20260213", "000000", "2026-02-13"),
    ("20260213", None, "2026-02-13"),
])
def test_iso_datetime(d, t, expected):
    assert cc.iso_datetime(d, t) == expected


@pytest.mark.parametrize("start,end,expected", [
    ("2026-01", "2026-10", ("20260101", "20261031")),
    ("2025-12", "2026-02", ("20251201", "20260228")),
    ("2024-02", "2024-02", ("20240201", "20240229")),
    ("2026-12", "2026-12", ("20261201", "20261231")),
])
def test_month_bounds(start, end, expected):
    assert cc.month_bounds(start, end) == expected


def test_month_bounds_rejects_reversed_period():
    with pytest.raises(click.BadParameter):
        cc.month_bounds("2026-05", "2026-01")


@pytest.mark.parametrize("bad", ["2026-13", "2026/01", "26-01", ""])
def test_yyyymm_rejects(bad):
    with pytest.raises(click.BadParameter):
        cc.yyyymm(bad)


def test_pdf_key_strips_caf_prefix_and_suffix():
    assert cc.pdf_key("751T0000000000000000000PDF") == "T0000000000000000000"


@pytest.mark.parametrize("bad", ["A751000000000A0000000A", "Dabc", "751T000"])
def test_pdf_key_rejects(bad):
    with pytest.raises(click.BadParameter):
        cc.pdf_key(bad)


def test_safe_name_replaces_path_separators():
    assert cc.safe_name("Attestation 01/2026: QF?") == "Attestation 01-2026- QF-"


# --- normalization ---------------------------------------------------------

def test_payments_latest_first():
    assert [p["date"] for p in cc.normalize_payments(PAIEMENTS)] == ["2026-11-06", "2026-10-05"]


def test_payments_amount_in_cents():
    assert cc.normalize_payments(PAIEMENTS)[0]["amount_cents"] == 132815


def test_payments_drop_bank_account():
    assert "compteVersement" not in json.dumps(cc.normalize_payments(PAIEMENTS))


def test_payments_benefit_label():
    assert cc.normalize_payments(PAIEMENTS)[1]["benefits"][0]["label"] == "Allocations familiales"


def test_droits_latest_month_first():
    assert [m["month"] for m in cc.normalize_droits(DROITS)] == ["2026-11", "2026-10"]


def test_droits_no_entitlement_reason():
    assert cc.normalize_droits(DROITS)[0]["no_entitlement"][0]["reason"] == "Ressources supérieures au plafond"


def test_droits_star_amount_is_none():
    assert cc.normalize_droits(DROITS)[0]["no_entitlement"][0]["amount_cents"] is None


def test_messages_latest_first():
    assert [m["kind"] for m in cc.normalize_messages(COURRIERS)] == ["declaration", "courrier", "courriel"]


def test_message_id_is_cle_piece():
    assert cc.normalize_messages(COURRIERS)[1]["id"] == "A751000000000A0000000A"


def test_declaration_id_is_stable():
    assert cc.normalize_messages(COURRIERS)[0]["id"] == cc.normalize_messages(COURRIERS)[0]["id"]


def test_declaration_id_prefix():
    assert cc.normalize_messages(COURRIERS)[0]["id"].startswith("D")


def test_message_subject_is_stripped():
    assert cc.normalize_messages(COURRIERS)[0]["subject"] == "Nous avons reçu les informations"


def test_message_errors_reports_failed_sections():
    assert cc.message_errors(COURRIERS) == ["emails"]


def test_demarche_id_is_receipt_address():
    assert cc.normalize_demarches(DEMARCHES)[0]["id"] == "751O0000000000000000000PDF"


def test_demarche_datetime():
    assert cc.normalize_demarches(DEMARCHES)[0]["date"] == "2026-10-11T14:57:08"


def test_profile_never_outputs_nir():
    assert "000000000000000" not in json.dumps(cc.normalize_profile(PROFIL, PROFIL_REDUIT))


def test_profile_household():
    assert cc.normalize_profile(PROFIL, PROFIL_REDUIT)["household"][0]["first_name"] == "LOU"


def test_profile_address_lines():
    assert cc.normalize_profile(PROFIL, PROFIL_REDUIT)["address"] == ["1 RUE DE L'EXEMPLE", "00000 VILLE", "FRANCE"]


# --- email payload ---------------------------------------------------------

def test_build_email_payload():
    assert cc.build_email("info_dos", "Bonjour\r\nMerci", "Caf de Test") == {
        "objetMessage": "INFO_DOS", "motifReclamation": None, "prestation": None,
        "msg": "Bonjour\nMerci", "nomCaf": "Caf de Test", "idSousMotifMessage": "RN_PRODUCT_125"}


def test_build_email_strips_control_chars():
    assert cc.build_email("INFO_DOS", "a\x07b", "X")["msg"] == "ab"


def test_build_email_reclamation_with_prestation():
    payload = cc.build_email("INFO_REC", "x", "X", motif="recl_pai", prestation="pres_paj")
    assert (payload["motifReclamation"], payload["prestation"]) == ("RECL_PAI", "PRES_PAJ")


def test_build_email_caf_motif_drops_prestation():
    assert cc.build_email("INFO_REC", "x", "X", motif="RECL_TEL", prestation="PRES_PAJ")["prestation"] is None


@pytest.mark.parametrize("kwargs", [
    {"topic": "NOPE", "body": "x"},
    {"topic": "INFO_DOS", "body": "   "},
    {"topic": "INFO_DOS", "body": "x" * 3001},
    {"topic": "INFO_REC", "body": "x"},
    {"topic": "INFO_REC", "body": "x", "motif": "RECL_PAI"},
    {"topic": "INFO_DOS", "body": "x", "motif": "RECL_PAI"},
])
def test_build_email_rejects(kwargs):
    with pytest.raises(click.BadParameter):
        cc.build_email(nom_caf="X", **kwargs)


# --- CLI wiring ------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["whoami", "--json"], ["profil", "--json"], ["paiements", "--json"], ["droits", "--json"],
    ["courriers", "--json"], ["courrier", "A751000000000B0000000B", "--json"], ["demarches", "--json"],
    ["topics", "--json"], ["send", "--topic", "INFO_DOS", "--body", "x", "--json"],
])
def test_json_output_parses(run, args):
    assert isinstance(json.loads(run(*args).output), (list, dict))


def test_paiements_since_filters(run):
    assert len(json.loads(run("paiements", "--since", "2026-11", "--json").output)) == 1


def test_droits_unknown_month_fails(run):
    assert "no droits for 2020-01" in run("droits", "--month", "2020-01").output


def test_courriers_kind_filter(run):
    assert [r["kind"] for r in json.loads(run("courriers", "--kind", "courriel", "--json").output)] == ["courriel"]


def test_courriers_unread_filter(run):
    assert [r["kind"] for r in json.loads(run("courriers", "--unread", "--json").output)] == ["courrier"]


def test_courrier_unknown_id_fails(run):
    assert "list them with `caf courriers`" in run("courrier", "A000").output


def test_courrier_text_shows_reply(run):
    assert "Réponse de la Caf: Votre courriel a été traité." in run("courrier", "A751000000000B0000000B").output


def test_download_by_message_id_uses_pdf_address(run, stub, tmp_path):
    run("download", "A751000000000A0000000A", "--target-dir", str(tmp_path))
    assert stub.pdfs == [cc.API["pdf"] + "T0000000000000000000"]


def test_download_names_file_from_listing(run, tmp_path):
    run("download", "751O0000000000000000000PDF", "--target-dir", str(tmp_path))
    assert [p.name for p in tmp_path.iterdir()] == [
        "2026-10-11 Déclaration de situation 751O0000000000000000000PDF.pdf"]


def test_download_email_has_no_pdf(run):
    assert "has no PDF" in run("download", "A751000000000B0000000B").output


def test_download_output_takes_one_id(run):
    assert run("download", "751O0000000000000000000PDF", "751T0000000000000000000PDF", "-o", "x.pdf").exit_code == 2


def test_attestation_period_path(run, stub, tmp_path):
    run("attestation", "qf", "--from", "2026-01", "--to", "2026-03", "--target-dir", str(tmp_path))
    assert stub.pdfs == [cc.API["attestation_period"] + "qf/20260101/20260331"]


def test_attestation_needs_both_bounds(run):
    assert run("attestation", "paiements", "--from", "2026-01").exit_code == 2


def test_attestation_net_social_uses_mns(run, stub, tmp_path):
    run("attestation", "net-social", "-o", str(tmp_path / "a.pdf"))
    assert stub.pdfs == [cc.API["attestation_last"] + "mns"]


def test_send_dry_run_does_not_post(run, stub):
    run("send", "--topic", "INFO_DOS", "--body", "x")
    assert stub.posts == []


def test_send_yes_posts_payload(run, stub):
    run("send", "--topic", "INFO_DOS", "--body", "x", "--yes")
    assert stub.posts == [(cc.API["envoi_mail"], cc.build_email("INFO_DOS", "x", "Caisse d'Allocations familiales de Test"))]


def test_send_needs_one_body(run):
    assert run("send", "--topic", "INFO_DOS").exit_code == 2


def test_no_chrome_names_the_fix():
    out = CliRunner().invoke(cc.cli, ["--cdp-url", "http://localhost:1", "--no-launch", "whoami"]).output
    assert "drop --no-launch or start Chrome" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", *sys.argv[1:]]))
