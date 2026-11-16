# caf-cli

**Visibility: public.**

Also a Claude Desktop extension: download `caf-cli.mcpb` from the
[latest release](https://github.com/ClementWalter/caf-cli/releases/latest)
or from <https://clementwalter.github.io/claude-extensions/>. In Claude, ask to
connect your Caf account: a Chrome window opens on the Caf login page, sign in
there.

Ton **Mon Compte caf.fr** (Caisse d'Allocations Familiales) depuis le terminal :

- **paiements** et **droits** mois par mois, avec le motif quand une prestation
  n'est pas due ;
- **courriers et courriels** de la Caf, **démarches** en ligne, **profil** du
  foyer ;
- téléchargement en PDF des **courriers**, des **récapitulatifs de démarches**,
  des **attestations** (paiement, quotient familial, montant net social) et du
  **relevé de compte** mensuel ;
- **écrire à sa Caf** par « Contacter ma Caf » (simulation sans `--yes`).

La Caf ne publie pas d'API pour les allocataires. La CLI utilise l'API JSON
privée des applications Angular de Mon Compte.

## Pourquoi ça passe par Chrome

wwwd.caf.fr est derrière un bot manager Radware : tout client HTTP qui n'est pas
un navigateur reçoit une redirection vers un challenge JavaScript, même avec un
jeton valide. La CLI pilote donc un **Chrome dédié** via le DevTools Protocol et
fait chaque appel par `fetch` dans un onglet connecté, avec le jeton de 5 minutes
que le portail délivre.

Le Chrome dédié (profil `~/.caf-chrome`, port 9225) démarre tout seul et garde
ses cookies d'un lancement à l'autre.

## Installation

```bash
git clone https://github.com/ClementWalter/caf-cli ~/Documents/caf-cli
uv run ~/Documents/claudine/scripts/link_clis.py   # met `caf` sur le PATH
caf connect                                         # ouvre la fenêtre ; connecte-toi dedans
```

Il faut [uv](https://docs.astral.sh/uv/) et Google Chrome. `bin/caf` est un lien
symbolique vers `caf_cli.py`, un script PEP 723 aux dépendances épinglées.

## Utilisation

```bash
caf whoami
caf paiements --limit 5
caf droits --no-entitlement
caf courriers --kind courrier
caf download A751000000000A0000000A
caf attestation qf
caf attestation paiements --from 2026-01 --to 2026-10
caf releve
caf topics
caf send --topic INFO_DOS --body-file question.txt         # simulation
caf send --topic INFO_DOS --body-file question.txt --yes   # envoi réel
```

Voir [SKILL.md](SKILL.md) pour le détail des commandes et les pièges connus.

## Tests

```bash
uv run --script tests/test_caf_cli.py
```

Tests hors ligne, sur des réponses d'API synthétiques qui reprennent la
structure des vraies.

## Privacy Policy

See https://clementwalter.github.io/claude-extensions/privacy/. The tool talks
only to wwwd.caf.fr, from a Chrome window on your machine, and writes only the
PDF files you ask it to download. It never stores your password.
