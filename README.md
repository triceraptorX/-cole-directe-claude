# ÉcoleDirecte × Claude

Serveur [MCP](https://modelcontextprotocol.io) qui connecte **ÉcoleDirecte** à **Claude** (Claude Desktop, Claude Code ou tout client MCP).
Une fois installé, tu peux demander à Claude par exemple :

- « Quelles sont mes dernières notes en maths ? Quelle est ma moyenne ce trimestre ? »
- « Qu'est-ce que j'ai à faire pour demain ? Marque l'exercice de physique comme fait. »
- « Quel est mon emploi du temps de la semaine prochaine ? Des cours annulés ? »
- « Résume-moi les messages non lus. »
- « Télécharge mon dernier bulletin. »

Fonctionne avec les comptes **élève** et **parent** (plusieurs enfants : précise le prénom).

> Basé sur la documentation non officielle de l'API ÉcoleDirecte. Ce projet n'est pas affilié à Aplim.

## Outils disponibles

| Outil | Description |
|---|---|
| `se_connecter` | Connexion et résumé du compte (établissement, élèves) |
| `repondre_qcm` | Répond au QCM de sécurité demandé par ÉcoleDirecte sur un nouvel appareil |
| `lister_eleves` | Élèves accessibles (utile pour les comptes parents) |
| `notes` | Notes, moyennes par matière (calculées si l'établissement les masque), filtres période/matière |
| `devoirs` | Travail à faire à venir, ou détail d'un jour (contenu de séance, pièces jointes) |
| `marquer_devoir` | Marque un devoir comme fait / non fait |
| `emploi_du_temps` | Cours sur une période (salles, profs, annulations) |
| `vie_scolaire` | Absences, retards, sanctions, encouragements |
| `actualites` | Fil d'actualité de l'élève et post-its de l'établissement |
| `messages` / `lire_message` | Messagerie : liste, recherche, lecture |
| `documents` | Documents administratifs (bulletins, certificats…) |
| `telecharger_fichier` | Télécharge un document / pièce jointe dans `~/Downloads/ecoledirecte` |

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/getting-started/installation/) (il installe Python automatiquement).

### Claude Desktop

Ouvre *Paramètres → Développeur → Modifier la configuration* (`claude_desktop_config.json`) et ajoute :

```json
{
  "mcpServers": {
    "ecoledirecte": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/triceraptorX/-cole-directe-claude", "ecoledirecte-mcp"],
      "env": {
        "ECOLEDIRECTE_IDENTIFIANT": "ton_identifiant",
        "ECOLEDIRECTE_MOT_DE_PASSE": "ton_mot_de_passe"
      }
    }
  }
}
```

Redémarre Claude Desktop : les outils ÉcoleDirecte apparaissent dans le menu des connecteurs.

### Claude Code

```bash
claude mcp add ecoledirecte \
  -e ECOLEDIRECTE_IDENTIFIANT=ton_identifiant \
  -e ECOLEDIRECTE_MOT_DE_PASSE=ton_mot_de_passe \
  -- uvx --from git+https://github.com/triceraptorX/-cole-directe-claude ecoledirecte-mcp
```

Ou, depuis un clone de ce dépôt, le fichier `.mcp.json` fourni lit les variables
`ECOLEDIRECTE_IDENTIFIANT` et `ECOLEDIRECTE_MOT_DE_PASSE` de ton environnement.

### Variables d'environnement

| Variable | Rôle |
|---|---|
| `ECOLEDIRECTE_IDENTIFIANT` | Identifiant ÉcoleDirecte (obligatoire) |
| `ECOLEDIRECTE_MOT_DE_PASSE` | Mot de passe (obligatoire) |
| `ECOLEDIRECTE_FICHIER_ETAT` | Où mémoriser la validation du QCM (défaut : `~/.config/ecoledirecte-mcp/etat.json`) |
| `ECOLEDIRECTE_TELECHARGEMENTS` | Dossier des fichiers téléchargés (défaut : `~/Downloads/ecoledirecte`) |

## Première connexion : le QCM de sécurité

Sur un nouvel appareil, ÉcoleDirecte pose une question de sécurité (ex. « Quelle est votre année de naissance ? »).
Claude te posera la question avec les propositions ; réponds-lui et il appellera `repondre_qcm`.
La validation (`cn`/`cv`) est ensuite enregistrée localement (fichier en `chmod 600`) : le QCM n'est plus demandé.

## Sécurité

- Les identifiants restent sur ta machine : le serveur tourne en local et ne parle qu'à `api.ecoledirecte.com`.
- Le seul outil qui modifie quelque chose sur ÉcoleDirecte est `marquer_devoir`. Lire un message le marque comme lu.

## Développement

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

Les tests utilisent un faux serveur ÉcoleDirecte (`httpx.MockTransport`) : aucun compte n'est nécessaire.
Pour inspecter le serveur à la main : `npx @modelcontextprotocol/inspector uv run ecoledirecte-mcp`.
