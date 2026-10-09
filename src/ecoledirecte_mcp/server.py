"""Serveur MCP exposant ÉcoleDirecte à Claude."""

from __future__ import annotations

import functools
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeVar

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import Compte, EcoleDirecteClient, EcoleDirecteError, Eleve
from .utils import b64_html_to_text, parse_fr_number

INSTRUCTIONS = """\
Accès au compte ÉcoleDirecte de l'utilisateur (élève ou parent) : notes, devoirs,
emploi du temps, vie scolaire, messagerie et documents.

- Les dates sont au format AAAA-MM-JJ.
- Pour un compte parent avec plusieurs enfants, passe le prénom ou l'id de l'enfant
  dans le paramètre `eleve` (voir `lister_eleves`). Sans ce paramètre, le premier enfant est utilisé.
- Si un outil répond qu'un QCM de sécurité est requis, pose la question à l'utilisateur
  avec les propositions, puis appelle `repondre_qcm` avec sa réponse. Ne devine jamais la réponse.
"""

mcp = MCPServer("ecoledirecte", instructions=INSTRUCTIONS)

LECTURE = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

_client: EcoleDirecteClient | None = None


def get_client() -> EcoleDirecteClient:
    global _client
    if _client is None:
        identifiant = os.environ.get("ECOLEDIRECTE_IDENTIFIANT")
        mot_de_passe = os.environ.get("ECOLEDIRECTE_MOT_DE_PASSE")
        if not identifiant or not mot_de_passe:
            raise ToolError(
                "Identifiants manquants : définis les variables d'environnement "
                "ECOLEDIRECTE_IDENTIFIANT et ECOLEDIRECTE_MOT_DE_PASSE dans la configuration du serveur MCP."
            )
        _client = EcoleDirecteClient(identifiant, mot_de_passe, fichier_etat=_fichier_etat())
    return _client


def set_client(client: EcoleDirecteClient | None) -> None:
    """Permet d'injecter un client (tests)."""
    global _client
    _client = client


def _fichier_etat() -> Path:
    if chemin := os.environ.get("ECOLEDIRECTE_FICHIER_ETAT"):
        return Path(chemin).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ecoledirecte-mcp" / "etat.json"


def _dossier_telechargements() -> Path:
    if chemin := os.environ.get("ECOLEDIRECTE_TELECHARGEMENTS"):
        return Path(chemin).expanduser()
    return Path.home() / "Downloads" / "ecoledirecte"


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def outil(func: F) -> F:
    """Transforme les erreurs ÉcoleDirecte/HTTP en erreurs d'outil lisibles par Claude."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except EcoleDirecteError as exc:
            raise ToolError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"Impossible de joindre ÉcoleDirecte : {exc}") from exc

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------- helpers


async def _compte() -> Compte:
    return await get_client().compte_connecte()


async def _eleve(eleve: str | int | None) -> Eleve:
    compte = await _compte()
    if not compte.eleves:
        raise ToolError("Aucun élève n'est rattaché à ce compte.")
    if eleve is None or str(eleve).strip() == "":
        return compte.eleves[0]
    cle = str(eleve).strip().lower()
    for e in compte.eleves:
        if cle in {str(e.id), e.prenom.lower(), f"{e.prenom} {e.nom}".lower()}:
            return e
    noms = ", ".join(f"{e.prenom} (id {e.id})" for e in compte.eleves)
    raise ToolError(f"Élève « {eleve} » introuvable. Élèves disponibles : {noms}")


def _date(valeur: str | None, defaut: date) -> date:
    if not valeur:
        return defaut
    try:
        return date.fromisoformat(valeur)
    except ValueError as exc:
        raise ToolError(f"Date invalide « {valeur} » : utilise le format AAAA-MM-JJ.") from exc


def _sans_vides(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------- connexion


@mcp.tool(annotations=LECTURE)
@outil
async def se_connecter() -> dict[str, Any]:
    """Se connecte à ÉcoleDirecte et renvoie un résumé du compte (type, établissement, élèves).

    Utile pour vérifier que la connexion fonctionne. Si un QCM de sécurité est demandé,
    l'erreur contient la question et les propositions à soumettre à l'utilisateur.
    """
    compte = await get_client().connecter()
    return {
        "compte": f"{compte.prenom} {compte.nom}".strip(),
        "type": "parent" if compte.est_parent else "élève",
        "etablissement": compte.etablissement,
        "annee_scolaire": compte.annee_scolaire,
        "eleves": [e.resume() for e in compte.eleves],
    }


@mcp.tool()
@outil
async def repondre_qcm(reponse: str) -> dict[str, Any]:
    """Répond au QCM de sécurité d'ÉcoleDirecte puis termine la connexion.

    La réponse est mémorisée localement, le QCM ne sera plus demandé ensuite sur cette machine.

    Args:
        reponse: Le texte de la proposition choisie par l'utilisateur, ou son numéro (1, 2, ...).
    """
    compte = await get_client().repondre_qcm(reponse)
    return {
        "connecte": True,
        "compte": f"{compte.prenom} {compte.nom}".strip(),
        "eleves": [e.resume() for e in compte.eleves],
    }


@mcp.tool(annotations=LECTURE)
@outil
async def lister_eleves() -> list[dict[str, Any]]:
    """Liste les élèves accessibles avec ce compte (un seul pour un compte élève)."""
    compte = await _compte()
    return [{**e.resume(), "modules_actifs": e.modules} for e in compte.eleves]


# ---------------------------------------------------------------- notes


@mcp.tool(annotations=LECTURE)
@outil
async def notes(
    eleve: str | None = None,
    periode: str | None = None,
    matiere: str | None = None,
    annee_scolaire: str | None = None,
) -> dict[str, Any]:
    """Récupère les notes et moyennes d'un élève.

    Args:
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
        periode: Code ou nom de période ("A001", "1er Trimestre", "Année"...). Par défaut la
            période en cours (ou la dernière période non annuelle).
        matiere: Filtre sur le nom ou le code de la matière (ex. "maths", "ANGLAIS").
        annee_scolaire: Année au format "2024-2025" pour consulter une année passée.
    """
    e = await _eleve(eleve)
    data = await get_client().requete(
        f"/v3/eleves/{e.id}/notes.awp", data={"anneeScolaire": annee_scolaire or ""}
    )
    periodes = data.get("periodes") or []
    choisie = _choisir_periode(periodes, periode)
    code_periode = choisie.get("codePeriode") if choisie else None

    def match_matiere(*noms: str | None) -> bool:
        if not matiere:
            return True
        cible = matiere.lower()
        return any(n and cible in n.lower() for n in noms)

    toutes_notes = [
        n
        for n in data.get("notes") or []
        if (not code_periode or choisie.get("annuel") or n.get("codePeriode") == code_periode)
        and match_matiere(n.get("libelleMatiere"), n.get("codeMatiere"))
    ]
    toutes_notes.sort(key=lambda n: n.get("date") or "", reverse=True)

    notes_par_matiere: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in toutes_notes:
        notes_par_matiere[n.get("codeMatiere") or ""].append(n)

    ensemble = (choisie or {}).get("ensembleMatieres") or {}
    disciplines = []
    for d in ensemble.get("disciplines") or []:
        if d.get("groupeMatiere") or not match_matiere(d.get("discipline"), d.get("codeMatiere")):
            continue
        moyenne = d.get("moyenne") or None
        calculee = None
        if not moyenne:
            calculee = _moyenne_calculee(notes_par_matiere.get(d.get("codeMatiere") or "", []))
        disciplines.append(
            _sans_vides(
                {
                    "matiere": d.get("discipline"),
                    "code": d.get("codeMatiere"),
                    "moyenne": moyenne,
                    "moyenne_calculee": calculee,
                    "moyenne_classe": d.get("moyenneClasse"),
                    "min": d.get("moyenneMin"),
                    "max": d.get("moyenneMax"),
                    "coef": d.get("coef"),
                    "rang": d.get("rang") or None,
                    "professeurs": [p.get("nom") for p in d.get("professeurs") or []],
                }
            )
        )

    return _sans_vides(
        {
            "eleve": f"{e.prenom} {e.nom}",
            "periode": (choisie or {}).get("periode"),
            "code_periode": code_periode,
            "periodes_disponibles": [
                f"{p.get('codePeriode')} : {p.get('periode')}" for p in periodes
            ],
            "moyenne_generale": ensemble.get("moyenneGenerale"),
            "moyenne_generale_classe": ensemble.get("moyenneClasse"),
            "disciplines": disciplines,
            "notes": [
                _sans_vides(
                    {
                        "date": n.get("date"),
                        "matiere": n.get("libelleMatiere"),
                        "devoir": n.get("devoir"),
                        "type": n.get("typeDevoir"),
                        "note": n.get("valeur"),
                        "sur": n.get("noteSur"),
                        "coef": n.get("coef"),
                        "non_significatif": n.get("nonSignificatif") or None,
                        "moyenne_classe": n.get("moyenneClasse"),
                        "min_classe": n.get("minClasse"),
                        "max_classe": n.get("maxClasse"),
                        "commentaire": n.get("commentaire"),
                        "competences": [
                            f"{c.get('libelleElementProgramme') or c.get('descriptif')} : {c.get('valeur')}"
                            for c in n.get("elementsProgramme") or []
                        ],
                    }
                )
                for n in toutes_notes
            ],
        }
    )


def _choisir_periode(periodes: list[dict[str, Any]], demande: str | None) -> dict[str, Any] | None:
    if not periodes:
        return None
    if demande:
        cible = demande.strip().lower()
        for p in periodes:
            if cible in {str(p.get("codePeriode", "")).lower(), str(p.get("periode", "")).lower()}:
                return p
        for p in periodes:
            if cible in str(p.get("periode", "")).lower():
                return p
        raise ToolError(
            f"Période « {demande} » introuvable. Périodes : "
            + ", ".join(f"{p.get('codePeriode')} ({p.get('periode')})" for p in periodes)
        )
    aujourdhui = date.today().isoformat()
    non_annuelles = [p for p in periodes if not p.get("annuel")]
    for p in non_annuelles:
        if (p.get("dateDebut") or "") <= aujourdhui <= (p.get("dateFin") or ""):
            return p
    passees = [p for p in non_annuelles if (p.get("dateDebut") or "") <= aujourdhui]
    return (passees or non_annuelles or periodes)[-1]


def _moyenne_calculee(notes_matiere: list[dict[str, Any]]) -> str | None:
    total = poids = 0.0
    for n in notes_matiere:
        if n.get("nonSignificatif"):
            continue
        valeur = parse_fr_number(n.get("valeur"))
        sur = parse_fr_number(n.get("noteSur"))
        coef = parse_fr_number(n.get("coef")) or 1.0
        if valeur is None or not sur:
            continue
        total += valeur / sur * 20 * coef
        poids += coef
    return f"{total / poids:.2f}".replace(".", ",") if poids else None


# ---------------------------------------------------------------- devoirs


@mcp.tool(annotations=LECTURE)
@outil
async def devoirs(
    eleve: str | None = None,
    date_jour: str | None = None,
    jours: int = 14,
) -> dict[str, Any]:
    """Récupère le travail à faire (cahier de texte) avec le détail de chaque devoir.

    Args:
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
        date_jour: Un jour précis (AAAA-MM-JJ) : renvoie les devoirs à rendre ce jour-là et le
            contenu des séances. Sans date, renvoie les devoirs à venir.
        jours: Sans `date_jour`, limite aux devoirs à rendre dans les N prochains jours.
    """
    e = await _eleve(eleve)
    client = get_client()
    if date_jour:
        jour = _date(date_jour, date.today()).isoformat()
        return {"eleve": f"{e.prenom} {e.nom}", "jours": [await _cahier_jour(client, e, jour)]}

    data = await client.requete(f"/v3/Eleves/{e.id}/cahierdetexte.awp") or {}
    limite = (date.today() + timedelta(days=max(jours, 0))).isoformat()
    dates = sorted(d for d in data if d <= limite)
    return {
        "eleve": f"{e.prenom} {e.nom}",
        "jours": [await _cahier_jour(client, e, d, seulement_a_faire=True) for d in dates],
    }


async def _cahier_jour(
    client: EcoleDirecteClient, e: Eleve, jour: str, *, seulement_a_faire: bool = False
) -> dict[str, Any]:
    data = await client.requete(f"/v3/Eleves/{e.id}/cahierdetexte/{jour}.awp") or {}
    items = []
    for m in data.get("matieres") or []:
        a_faire = m.get("aFaire") or {}
        seance = m.get("contenuDeSeance") or {}
        if seulement_a_faire and not a_faire:
            continue
        item: dict[str, Any] = {
            "matiere": m.get("matiere"),
            "professeur": m.get("nomProf"),
            "interrogation": m.get("interrogation") or None,
        }
        if a_faire:
            item["a_faire"] = _sans_vides(
                {
                    "id_devoir": a_faire.get("idDevoir"),
                    "contenu": b64_html_to_text(a_faire.get("contenu")),
                    "donne_le": a_faire.get("donneLe"),
                    "effectue": a_faire.get("effectue"),
                    "rendre_en_ligne": a_faire.get("rendreEnLigne") or None,
                    "pieces_jointes": _pieces_jointes(a_faire.get("documents")),
                }
            )
        if not seulement_a_faire and seance.get("contenu"):
            item["contenu_de_seance"] = _sans_vides(
                {
                    "contenu": b64_html_to_text(seance.get("contenu")),
                    "pieces_jointes": _pieces_jointes(seance.get("documents")),
                }
            )
        items.append(_sans_vides(item))
    return {"date": jour, "matieres": items}


def _pieces_jointes(documents: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"id": d.get("id"), "nom": d.get("libelle"), "type": d.get("type")}
        for d in documents or []
    ]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@outil
async def marquer_devoir(id_devoir: int, effectue: bool = True, eleve: str | None = None) -> str:
    """Marque un devoir comme fait (ou non fait) dans le cahier de texte.

    Args:
        id_devoir: Identifiant du devoir (champ `id_devoir` renvoyé par l'outil `devoirs`).
        effectue: True pour « fait », False pour « non fait ».
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
    """
    e = await _eleve(eleve)
    await get_client().requete(
        f"/v3/Eleves/{e.id}/cahierdetexte.awp",
        verbe="put",
        data={
            "idDevoirsEffectues": [id_devoir] if effectue else [],
            "idDevoirsNonEffectues": [] if effectue else [id_devoir],
        },
    )
    return f"Devoir {id_devoir} marqué comme {'fait' if effectue else 'non fait'}."


# ---------------------------------------------------------------- emploi du temps


@mcp.tool(annotations=LECTURE)
@outil
async def emploi_du_temps(
    date_debut: str | None = None,
    date_fin: str | None = None,
    eleve: str | None = None,
) -> dict[str, Any]:
    """Récupère l'emploi du temps sur une période (cours, salles, profs, annulations).

    Args:
        date_debut: Premier jour (AAAA-MM-JJ). Par défaut aujourd'hui.
        date_fin: Dernier jour inclus (AAAA-MM-JJ). Par défaut égal à `date_debut`.
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
    """
    e = await _eleve(eleve)
    debut = _date(date_debut, date.today())
    fin = _date(date_fin, debut)
    if fin < debut:
        raise ToolError("`date_fin` doit être postérieure à `date_debut`.")
    data = await get_client().requete(
        f"/v3/E/{e.id}/emploidutemps.awp",
        data={"dateDebut": debut.isoformat(), "dateFin": fin.isoformat(), "avecTrous": False},
    )
    cours = sorted(data or [], key=lambda c: c.get("start_date") or "")
    par_jour: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cours:
        debut_c = c.get("start_date") or ""
        par_jour[debut_c[:10]].append(
            _sans_vides(
                {
                    "debut": debut_c[11:],
                    "fin": (c.get("end_date") or "")[11:],
                    "matiere": c.get("matiere") or c.get("text"),
                    "type": c.get("typeCours"),
                    "professeur": c.get("prof"),
                    "salle": c.get("salle"),
                    "groupe": c.get("groupe") or c.get("classe"),
                    "annule": c.get("isAnnule") or None,
                    "modifie": c.get("isModifie") or None,
                    "dispense": bool(c.get("dispense")) or None,
                }
            )
        )
    return {"eleve": f"{e.prenom} {e.nom}", "jours": [{"date": j, "cours": cs} for j, cs in par_jour.items()]}


# ---------------------------------------------------------------- vie scolaire


@mcp.tool(annotations=LECTURE)
@outil
async def vie_scolaire(eleve: str | None = None) -> dict[str, Any]:
    """Absences, retards, sanctions et encouragements de l'élève.

    Args:
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
    """
    e = await _eleve(eleve)
    data = await get_client().requete(f"/v3/eleves/{e.id}/viescolaire.awp") or {}

    def formater(item: dict[str, Any]) -> dict[str, Any]:
        return _sans_vides(
            {
                "type": item.get("typeElement"),
                "date": item.get("date"),
                "detail": item.get("displayDate"),
                "libelle": item.get("libelle"),
                "motif": item.get("motif"),
                "justifie": item.get("justifie"),
                "justification": (item.get("typeJustification") or "").strip(),
                "par": item.get("par"),
                "commentaire": item.get("commentaire"),
                "a_faire": item.get("aFaire"),
                "deroulement": (item.get("dateDeroulement") or "").replace("<BR>", " "),
            }
        )

    return {
        "eleve": f"{e.prenom} {e.nom}",
        "absences_retards": [formater(i) for i in data.get("absencesRetards") or []],
        "sanctions_encouragements": [formater(i) for i in data.get("sanctionsEncouragements") or []],
    }


# ---------------------------------------------------------------- timeline


@mcp.tool(annotations=LECTURE)
@outil
async def actualites(eleve: str | None = None, nombre: int = 30) -> dict[str, Any]:
    """Fil d'actualité : derniers événements de l'élève (notes, absences, documents, messages)
    et post-its de l'établissement.

    Args:
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
        nombre: Nombre maximum d'événements personnels à renvoyer.
    """
    e = await _eleve(eleve)
    client = get_client()
    timeline = await client.requete(f"/v3/eleves/{e.id}/timeline.awp") or []
    try:
        commune = await client.requete(f"/v3/E/{e.id}/timelineAccueilCommun.awp") or {}
    except EcoleDirecteError:
        commune = {}
    return {
        "eleve": f"{e.prenom} {e.nom}",
        "evenements": [
            _sans_vides(
                {
                    "date": t.get("date"),
                    "type": t.get("typeElement"),
                    "titre": t.get("titre"),
                    "sous_titre": t.get("soustitre"),
                    "contenu": t.get("contenu"),
                }
            )
            for t in timeline[: max(nombre, 0)]
        ],
        "post_its_etablissement": [
            _sans_vides(
                {
                    "du": p.get("dateDebut"),
                    "au": p.get("dateFin"),
                    "auteur": " ".join(
                        filter(None, [(p.get("auteur") or {}).get(k) for k in ("civilite", "prenom", "nom")])
                    ),
                    "contenu": b64_html_to_text(p.get("contenu")),
                }
            )
            for p in commune.get("postits") or []
        ],
    }


# ---------------------------------------------------------------- messagerie


def _route_messages(compte: Compte, e: Eleve) -> str:
    return f"/v3/familles/{compte.id}" if compte.est_parent else f"/v3/eleves/{e.id}"


def _personne(p: dict[str, Any] | None) -> str:
    p = p or {}
    return " ".join(filter(None, [p.get("civilite"), p.get("prenom"), p.get("particule"), p.get("nom")]))


@mcp.tool(annotations=LECTURE)
@outil
async def messages(
    dossier: Literal["received", "sent", "archived", "draft"] = "received",
    recherche: str | None = None,
    non_lus_seulement: bool = False,
    page: int = 0,
    par_page: int = 20,
    annee_scolaire: str | None = None,
    eleve: str | None = None,
) -> dict[str, Any]:
    """Liste les messages de la messagerie ÉcoleDirecte (sans leur contenu).

    Args:
        dossier: "received" (reçus), "sent" (envoyés), "archived" (archivés) ou "draft" (brouillons).
        recherche: Texte à rechercher dans les messages.
        non_lus_seulement: Ne renvoyer que les messages non lus.
        page: Numéro de page (commence à 0).
        par_page: Nombre de messages par page.
        annee_scolaire: Année au format "2024-2025" (par défaut l'année en cours).
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
    """
    compte = await _compte()
    e = await _eleve(eleve)
    data = await get_client().requete(
        f"{_route_messages(compte, e)}/messages.awp",
        params={
            "force": "false",
            "typeRecuperation": dossier,
            "idClasseur": 0,
            "orderBy": "date",
            "order": "desc",
            "query": recherche or "",
            "onlyRead": "",
            "page": page,
            "itemsPerPage": par_page,
            "getAll": 0,
        },
        data={"anneeMessages": annee_scolaire or compte.annee_scolaire},
    ) or {}
    liste = (data.get("messages") or {}).get(dossier) or []
    if non_lus_seulement:
        liste = [m for m in liste if not m.get("read")]
    return {
        "dossier": dossier,
        "pagination": data.get("pagination"),
        "messages": [
            _sans_vides(
                {
                    "id": m.get("id"),
                    "date": m.get("date"),
                    "sujet": m.get("subject"),
                    "de": _personne(m.get("from")),
                    "a": [_personne(t) for t in m.get("to") or []],
                    "lu": m.get("read"),
                    "pieces_jointes": len(m.get("files") or []) or None,
                }
            )
            for m in liste
        ],
    }


@mcp.tool(annotations=LECTURE)
@outil
async def lire_message(
    id_message: int,
    dossier: Literal["received", "sent", "archived", "draft"] = "received",
    annee_scolaire: str | None = None,
    eleve: str | None = None,
) -> dict[str, Any]:
    """Lit le contenu complet d'un message (le message sera marqué comme lu).

    Args:
        id_message: Identifiant du message (champ `id` renvoyé par l'outil `messages`).
        dossier: Dossier du message ; "sent" pour un message envoyé.
        annee_scolaire: Année au format "2024-2025" si le message date d'une année passée.
        eleve: Prénom ou id de l'élève (comptes parents uniquement).
    """
    compte = await _compte()
    e = await _eleve(eleve)
    m = await get_client().requete(
        f"{_route_messages(compte, e)}/messages/{id_message}.awp",
        params={"mode": "expediteur" if dossier in {"sent", "draft"} else "destinataire"},
        data={"anneeMessages": annee_scolaire or compte.annee_scolaire},
    ) or {}
    return _sans_vides(
        {
            "id": m.get("id"),
            "date": m.get("date"),
            "sujet": m.get("subject"),
            "de": _personne(m.get("from")),
            "a": [_personne(t) for t in m.get("to") or []],
            "contenu": b64_html_to_text(m.get("content")),
            "pieces_jointes": [
                {"id": f.get("id"), "nom": f.get("libelle"), "type": f.get("type")}
                for f in m.get("files") or []
            ],
        }
    )


# ---------------------------------------------------------------- documents


@mcp.tool(annotations=LECTURE)
@outil
async def documents(annee_scolaire: str | None = None) -> dict[str, Any]:
    """Liste les documents administratifs (bulletins, certificats, documents de vie scolaire...).

    Args:
        annee_scolaire: Année au format "2023-2024" pour les archives (par défaut l'année en cours).
    """
    data = await get_client().requete(
        "/v3/elevesDocuments.awp", params={"archive": annee_scolaire or ""}
    ) or {}
    resultat: dict[str, Any] = {}
    for categorie in ("notes", "administratifs", "viescolaire", "factures", "inscriptions"):
        docs = data.get(categorie) or []
        if docs:
            resultat[categorie] = [
                _sans_vides(
                    {
                        "id": d.get("id"),
                        "nom": d.get("libelle"),
                        "date": d.get("date"),
                        "type": d.get("type"),
                        "id_eleve": d.get("idEleve"),
                    }
                )
                for d in docs
            ]
    return resultat or {"documents": [], "info": "Aucun document."}


TypeFichier = Literal["DOCUMENT", "FICHIER_CDT", "PIECE_JOINTE", "CLOUD", "FICHIER_MENU_RESTAURATION"]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
@outil
async def telecharger_fichier(
    fichier_id: str,
    type_fichier: TypeFichier = "DOCUMENT",
    nom: str | None = None,
    annee_scolaire: str | None = None,
) -> dict[str, Any]:
    """Télécharge un fichier ÉcoleDirecte sur l'ordinateur et renvoie son chemin local.

    Args:
        fichier_id: Identifiant du fichier (id d'un document, d'une pièce jointe ou chemin cloud).
        type_fichier: "DOCUMENT" (documents administratifs), "FICHIER_CDT" (pièce jointe du
            cahier de texte), "PIECE_JOINTE" (pièce jointe d'un message), "CLOUD" ou
            "FICHIER_MENU_RESTAURATION".
        nom: Nom de fichier à utiliser (optionnel).
        annee_scolaire: Pour un document archivé ou une pièce jointe d'un ancien message ("2023-2024").
    """
    params: dict[str, Any] = {"fichierId": fichier_id}
    if type_fichier == "DOCUMENT":
        if annee_scolaire:
            params.update({"archive": "true", "anneeArchive": annee_scolaire})
    else:
        params["leTypeDeFichier"] = type_fichier
        if type_fichier == "PIECE_JOINTE" and annee_scolaire:
            params["anneeMessages"] = annee_scolaire
    contenu, mime, nom_serveur = await get_client().telecharger(params)
    nom_final = _nom_sur(nom or nom_serveur or f"ecoledirecte-{fichier_id}")
    if "." not in nom_final and mime and "pdf" in mime:
        nom_final += ".pdf"
    dossier = _dossier_telechargements()
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / nom_final
    chemin.write_bytes(contenu)
    return {"chemin": str(chemin), "taille_octets": len(contenu), "type": mime}


def _nom_sur(nom: str) -> str:
    nom = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", nom).strip(" .")
    return nom[:150] or "fichier"


def main() -> None:
    mcp.run()


__all__ = ["mcp", "main", "set_client"]
