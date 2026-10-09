"""Client asynchrone pour l'API (non officielle) d'ÉcoleDirecte.

Gère :
- la récupération du cookie GTK avant le login ;
- le login, y compris la double authentification par QCM (code 250) ;
- la mémorisation des jetons ``cn``/``cv`` pour éviter de refaire le QCM ;
- le renouvellement automatique du token (codes 520/525).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .utils import b64_decode

BASE_URL = "https://api.ecoledirecte.com"
API_VERSION = "4.75.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
)

CODE_OK = 200
CODE_QCM = 250
CODE_IDENTIFIANTS_INVALIDES = 505
CODES_TOKEN_INVALIDE = {520, 525}

ERREURS_CONNUES = {
    505: "Identifiant et/ou mot de passe invalide.",
    517: "Version de l'API invalide.",
    520: "Token invalide.",
    525: "Token expiré.",
    535: "L'établissement n'est pas disponible sur ÉcoleDirecte (vacances scolaires ?).",
    40129: "Format JSON invalide.",
}


class EcoleDirecteError(Exception):
    """Erreur renvoyée par l'API ÉcoleDirecte."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class QCMRequired(EcoleDirecteError):
    """La connexion nécessite de répondre au QCM de sécurité."""

    def __init__(self, question: str, propositions: list[str]) -> None:
        lignes = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(propositions))
        super().__init__(
            "ÉcoleDirecte demande de confirmer l'identité (nouvel appareil).\n"
            f"Question : {question}\nPropositions :\n{lignes}\n"
            "Demande la bonne réponse à l'utilisateur puis appelle l'outil "
            "`repondre_qcm` avec le texte (ou le numéro) de la proposition choisie.",
            CODE_QCM,
        )
        self.question = question
        self.propositions = propositions


@dataclass
class Eleve:
    id: int
    prenom: str
    nom: str
    classe: str = ""
    modules: list[str] = field(default_factory=list)

    def resume(self) -> dict[str, Any]:
        return {"id": self.id, "prenom": self.prenom, "nom": self.nom, "classe": self.classe}


@dataclass
class Compte:
    id: int
    type_compte: str
    prenom: str
    nom: str
    etablissement: str
    annee_scolaire: str
    eleves: list[Eleve]
    brut: dict[str, Any]

    @property
    def est_parent(self) -> bool:
        return self.type_compte != "E"


def _modules_actifs(account_or_eleve: dict[str, Any]) -> list[str]:
    return [m["code"] for m in account_or_eleve.get("modules", []) if m.get("enable")]


def parse_compte(account: dict[str, Any]) -> Compte:
    profile = account.get("profile") or {}
    if account.get("typeCompte") == "E":
        eleves = [
            Eleve(
                id=account["id"],
                prenom=account.get("prenom", ""),
                nom=account.get("nom", ""),
                classe=(profile.get("classe") or {}).get("libelle", ""),
                modules=_modules_actifs(account),
            )
        ]
    else:
        eleves = [
            Eleve(
                id=e["id"],
                prenom=e.get("prenom", ""),
                nom=e.get("nom", ""),
                classe=(e.get("classe") or {}).get("libelle", ""),
                modules=_modules_actifs(e),
            )
            for e in profile.get("eleves", [])
        ]
    return Compte(
        id=account["id"],
        type_compte=account.get("typeCompte", ""),
        prenom=account.get("prenom", ""),
        nom=account.get("nom", ""),
        etablissement=account.get("nomEtablissement") or profile.get("nomEtablissement", ""),
        annee_scolaire=account.get("anneeScolaireCourante", ""),
        eleves=eleves,
        brut=account,
    )


class EcoleDirecteClient:
    def __init__(
        self,
        identifiant: str,
        mot_de_passe: str,
        *,
        fichier_etat: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.identifiant = identifiant
        self.mot_de_passe = mot_de_passe
        self.fichier_etat = fichier_etat
        self.token: str | None = None
        self.compte: Compte | None = None
        self._qcm_en_attente: tuple[str, list[str], list[str]] | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
            transport=transport,
            timeout=30.0,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ état

    def _charger_fa(self) -> list[dict[str, str]]:
        if not self.fichier_etat or not self.fichier_etat.exists():
            return []
        try:
            etat = json.loads(self.fichier_etat.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return etat.get(self.identifiant, {}).get("fa", [])

    def _sauver_fa(self, fa: list[dict[str, str]]) -> None:
        if not self.fichier_etat:
            return
        etat: dict[str, Any] = {}
        if self.fichier_etat.exists():
            try:
                etat = json.loads(self.fichier_etat.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                etat = {}
        etat.setdefault(self.identifiant, {})["fa"] = fa
        self.fichier_etat.parent.mkdir(parents=True, exist_ok=True)
        self.fichier_etat.write_text(json.dumps(etat, indent=2), "utf-8")
        try:
            os.chmod(self.fichier_etat, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ bas niveau

    async def _post(
        self,
        path: str,
        *,
        verbe: str | None = "get",
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if verbe:
            query["verbe"] = verbe
        query["v"] = API_VERSION
        query.update(params or {})
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        if auth and self.token:
            hdrs["X-Token"] = self.token
        hdrs.update(headers or {})
        body = "data=" + json.dumps(data or {}, ensure_ascii=False)
        resp = await self._http.post(path, params=query, content=body.encode("utf-8"), headers=hdrs)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EcoleDirecteError(f"Réponse inattendue d'ÉcoleDirecte : {resp.text[:200]}") from exc
        if payload.get("token"):
            self.token = payload["token"]
        return payload

    @staticmethod
    def _erreur(payload: dict[str, Any]) -> EcoleDirecteError:
        code = payload.get("code")
        message = payload.get("message") or ERREURS_CONNUES.get(code, "Erreur inconnue")
        return EcoleDirecteError(f"ÉcoleDirecte a renvoyé le code {code} : {message}", code)

    # ------------------------------------------------------------- connexion

    async def _recuperer_gtk(self) -> str | None:
        resp = await self._http.get("/v3/login.awp", params={"gtk": 1, "v": API_VERSION})
        return resp.cookies.get("GTK") or self._http.cookies.get("GTK")

    async def _login(self, fa: list[dict[str, str]]) -> dict[str, Any]:
        gtk = await self._recuperer_gtk()
        data: dict[str, Any] = {
            "identifiant": quote(self.identifiant, safe=""),
            "motdepasse": quote(self.mot_de_passe, safe=""),
            "isReLogin": False,
            "uuid": "",
        }
        if fa:
            data["fa"] = fa
        self.token = None
        return await self._post(
            "/v3/login.awp",
            verbe=None,
            data=data,
            headers={"X-Gtk": gtk} if gtk else None,
            auth=False,
        )

    async def connecter(self) -> Compte:
        """Se connecte (ou se reconnecte). Lève QCMRequired si un QCM doit être rempli."""
        async with self._lock:
            return await self._connecter()

    async def _connecter(self) -> Compte:
        payload = await self._login(self._charger_fa())
        code = payload.get("code")
        if code == CODE_QCM:
            await self._preparer_qcm()
            assert self._qcm_en_attente is not None
            question, propositions, _ = self._qcm_en_attente
            raise QCMRequired(question, propositions)
        if code != CODE_OK:
            raise self._erreur(payload)
        accounts = (payload.get("data") or {}).get("accounts") or []
        if not accounts:
            raise EcoleDirecteError("Aucun compte associé à ces identifiants.")
        principal = next((a for a in accounts if a.get("main")), accounts[0])
        self.compte = parse_compte(principal)
        self._qcm_en_attente = None
        return self.compte

    async def _preparer_qcm(self) -> None:
        payload = await self._post("/v3/connexion/doubleauth.awp", verbe="get", data={})
        if payload.get("code") != CODE_OK:
            raise self._erreur(payload)
        d = payload.get("data") or {}
        brutes = list(d.get("propositions") or [])
        self._qcm_en_attente = (
            b64_decode(d.get("question")),
            [b64_decode(p) for p in brutes],
            brutes,
        )

    async def repondre_qcm(self, reponse: str) -> Compte:
        """Répond au QCM (texte exact, approchant ou numéro de la proposition), puis se connecte."""
        async with self._lock:
            if self._qcm_en_attente is None:
                try:
                    return await self._connecter()
                except QCMRequired:
                    pass
            assert self._qcm_en_attente is not None
            question, propositions, brutes = self._qcm_en_attente
            index = _choisir_proposition(reponse, propositions)
            if index is None:
                raise QCMRequired(question, propositions)
            payload = await self._post(
                "/v3/connexion/doubleauth.awp", verbe="post", data={"choix": brutes[index]}
            )
            if payload.get("code") != CODE_OK:
                self._qcm_en_attente = None
                raise EcoleDirecteError(
                    "Mauvaise réponse au QCM (ou QCM expiré). Relance la connexion pour obtenir "
                    f"une nouvelle question. Détail : {payload.get('message') or payload.get('code')}",
                    payload.get("code"),
                )
            d = payload.get("data") or {}
            self._sauver_fa([{"cn": d.get("cn", ""), "cv": d.get("cv", "")}])
            self._qcm_en_attente = None
            return await self._connecter()

    async def compte_connecte(self) -> Compte:
        if self.compte is None or self.token is None:
            return await self.connecter()
        return self.compte

    # ---------------------------------------------------------------- requêtes

    async def requete(
        self,
        path: str,
        *,
        verbe: str = "get",
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Requête authentifiée ; renvoie la valeur de ``data`` de la réponse."""
        await self.compte_connecte()
        payload = await self._post(path, verbe=verbe, data=data, params=params)
        if payload.get("code") in CODES_TOKEN_INVALIDE:
            await self.connecter()
            payload = await self._post(path, verbe=verbe, data=data, params=params)
        if payload.get("code") != CODE_OK:
            raise self._erreur(payload)
        return payload.get("data")

    async def telecharger(self, params: dict[str, Any]) -> tuple[bytes, str | None, str | None]:
        """Télécharge un fichier ; renvoie (contenu, type MIME, nom de fichier proposé)."""
        await self.compte_connecte()
        for tentative in range(2):
            resp = await self._http.post(
                "/v3/telechargement.awp",
                params={"verbe": "get", "v": API_VERSION, **params},
                content=b'data={"forceDownload": 0}',
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Token": self.token or "",
                },
            )
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                payload = resp.json()
                if payload.get("code") in CODES_TOKEN_INVALIDE and tentative == 0:
                    await self.connecter()
                    continue
                raise self._erreur(payload)
            return resp.content, ctype or None, _nom_fichier(resp.headers.get("content-disposition"))
        raise EcoleDirecteError("Téléchargement impossible.")


def _nom_fichier(disposition: str | None) -> str | None:
    if not disposition:
        return None
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


def _normaliser(texte: str) -> str:
    import unicodedata

    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return " ".join(texte.lower().split())


def _choisir_proposition(reponse: str, propositions: list[str]) -> int | None:
    r = reponse.strip()
    if r.isdigit() and 1 <= int(r) <= len(propositions):
        return int(r) - 1
    cible = _normaliser(r)
    normalisees = [_normaliser(p) for p in propositions]
    if cible in normalisees:
        return normalisees.index(cible)
    partielles = [i for i, p in enumerate(normalisees) if cible and cible in p]
    if len(partielles) == 1:
        return partielles[0]
    return None
