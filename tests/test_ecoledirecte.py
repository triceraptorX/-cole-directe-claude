import asyncio
import base64
import json
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from ecoledirecte_mcp import server
from ecoledirecte_mcp.client import EcoleDirecteClient, EcoleDirecteError, QCMRequired
from ecoledirecte_mcp.utils import b64_decode, html_to_text


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


ACCOUNT = {
    "id": 1234,
    "typeCompte": "E",
    "main": True,
    "prenom": "John",
    "nom": "DOE",
    "nomEtablissement": "Lycée Github",
    "anneeScolaireCourante": "2026-2027",
    "modules": [{"code": "NOTES", "enable": True}, {"code": "CLOUD", "enable": False}],
    "profile": {"classe": {"id": 44, "code": "TB", "libelle": "Terminale B"}},
}


class FakeED:
    """Faux serveur ÉcoleDirecte suffisant pour les tests."""

    def __init__(self, qcm: bool = False) -> None:
        self.qcm = qcm
        self.token_counter = 0
        self.valid_tokens: set[str] = set()
        self.calls: list[tuple[str, str | None, dict]] = []
        self.expire_next = False

    def new_token(self) -> str:
        self.token_counter += 1
        tok = f"tok{self.token_counter}"
        self.valid_tokens.add(tok)
        return tok

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        verbe = request.url.params.get("verbe")
        body = request.content.decode()
        data = json.loads(body[len("data="):]) if body.startswith("data=") else {}
        self.calls.append((path, verbe, data))

        if request.method == "GET" and path == "/v3/login.awp":
            return httpx.Response(200, headers={"set-cookie": "GTK=gtkvalue; Path=/"}, json={})

        if path == "/v3/login.awp":
            assert request.headers.get("X-Gtk") == "gtkvalue"
            if unquote(data["motdepasse"]) != "p@ss&+word":
                return httpx.Response(200, json={"code": 505, "token": "", "message": "Mot de passe invalide !", "data": {"accounts": []}})
            if self.qcm and data.get("fa") != [{"cn": "CN", "cv": "CV"}]:
                return httpx.Response(200, json={"code": 250, "token": self.new_token(), "data": {}})
            return httpx.Response(200, json={"code": 200, "token": self.new_token(), "data": {"accounts": [ACCOUNT]}})

        token = request.headers.get("X-Token")
        if token not in self.valid_tokens:
            return httpx.Response(200, json={"code": 520, "token": "", "message": "Token invalide"})
        if self.expire_next:
            self.expire_next = False
            self.valid_tokens.clear()
            return httpx.Response(200, json={"code": 525, "token": "", "message": "Token expiré"})

        if path == "/v3/connexion/doubleauth.awp":
            if verbe == "get":
                return httpx.Response(200, json={"code": 200, "token": token, "data": {
                    "question": b64("Quelle est votre année de naissance ?"),
                    "propositions": [b64("2007"), b64("2008"), b64("2009")],
                }})
            if data.get("choix") == b64("2008"):
                return httpx.Response(200, json={"code": 200, "token": token, "data": {"cn": "CN", "cv": "CV"}})
            return httpx.Response(200, json={"code": 520, "token": "", "message": "Mauvaise réponse"})

        if path == "/v3/eleves/1234/notes.awp":
            return httpx.Response(200, json={"code": 200, "token": token, "data": NOTES})
        if path == "/v3/Eleves/1234/cahierdetexte.awp" and verbe == "get":
            return httpx.Response(200, json={"code": 200, "token": token, "data": {
                "2000-01-03": [{"matiere": "MATHS", "idDevoir": 9, "aFaire": True}],
            }})
        if path == "/v3/Eleves/1234/cahierdetexte.awp" and verbe == "put":
            return httpx.Response(200, json={"code": 200, "token": token, "data": {}})
        if path == "/v3/Eleves/1234/cahierdetexte/2000-01-03.awp":
            return httpx.Response(200, json={"code": 200, "token": token, "data": {"date": "2000-01-03", "matieres": [{
                "matiere": "MATHEMATIQUES", "nomProf": "M. X", "id": 9,
                "aFaire": {"idDevoir": 9, "contenu": b64("<p>Exercices&nbsp;1 &agrave; 3</p>"), "effectue": False,
                           "documents": [{"id": 77, "libelle": "sujet.pdf", "type": "FICHIER_CDT"}]},
            }]}})
        if path == "/v3/E/1234/emploidutemps.awp":
            return httpx.Response(200, json={"code": 200, "token": token, "data": [
                {"start_date": "2026-10-09 10:00", "end_date": "2026-10-09 10:55", "matiere": "ANGLAIS", "prof": "Mme Y", "salle": "S44", "isAnnule": True},
                {"start_date": "2026-10-09 08:00", "end_date": "2026-10-09 08:55", "matiere": "NSI", "prof": "M. Z", "salle": "S11", "isAnnule": False},
            ]})
        return httpx.Response(200, json={"code": 404, "token": token, "message": f"route inconnue {path}"})


NOTES = {
    "periodes": [
        {"codePeriode": "A001", "periode": "1er Trimestre", "annuel": False, "dateDebut": "2000-01-01", "dateFin": "2099-12-31",
         "ensembleMatieres": {"moyenneGenerale": "14,50", "disciplines": [
             {"codeMatiere": "MATHS", "discipline": "MATHEMATIQUES", "moyenne": "", "coef": 1, "professeurs": [{"nom": "M. X"}]},
             {"codeMatiere": "FRANC", "discipline": "FRANCAIS", "moyenne": "12,00"},
         ]}},
        {"codePeriode": "A999Z", "periode": "Année", "annuel": True, "dateDebut": "2000-01-01", "dateFin": "2099-12-31"},
    ],
    "notes": [
        {"codePeriode": "A001", "codeMatiere": "MATHS", "libelleMatiere": "MATHEMATIQUES", "devoir": "DS1", "valeur": "15", "noteSur": "20", "coef": "2", "date": "2026-09-20"},
        {"codePeriode": "A001", "codeMatiere": "MATHS", "libelleMatiere": "MATHEMATIQUES", "devoir": "Interro", "valeur": "8", "noteSur": "10", "coef": "1", "date": "2026-09-25"},
        {"codePeriode": "A001", "codeMatiere": "MATHS", "libelleMatiere": "MATHEMATIQUES", "devoir": "Absent", "valeur": "Abs", "noteSur": "20", "coef": "1", "date": "2026-09-26"},
        {"codePeriode": "A001", "codeMatiere": "FRANC", "libelleMatiere": "FRANCAIS", "devoir": "Dissert", "valeur": "12", "noteSur": "20", "coef": "1", "date": "2026-09-10"},
    ],
}


def make_client(fake: FakeED, tmp_path, password: str = "p@ss&+word") -> EcoleDirecteClient:
    return EcoleDirecteClient(
        "eleve", password, fichier_etat=tmp_path / "etat.json", transport=httpx.MockTransport(fake.handler)
    )


def run(coro):
    return asyncio.run(coro)


def call_tool(name: str, args: dict | None = None):
    result = run(server.mcp.call_tool(name, args or {}))
    return json.loads(result.content[0].text)


# ------------------------------------------------------------------ utils


def test_b64_decode_tolerates_bad_padding():
    assert b64_decode("NDI==") == "42"
    assert b64_decode("TGUgZnJvbWFnZQ==") == "Le fromage"


def test_html_to_text():
    assert html_to_text("<p>Bonjour&nbsp;<b>toi</b></p><ul><li>a</li><li>b</li></ul>") == "Bonjour toi\n\n- a\n\n- b"


# ------------------------------------------------------------------ client


def test_login_ok(tmp_path):
    fake = FakeED()
    client = make_client(fake, tmp_path)
    compte = run(client.connecter())
    assert compte.eleves[0].id == 1234
    assert compte.eleves[0].classe == "Terminale B"
    assert compte.eleves[0].modules == ["NOTES"]
    assert client.token == "tok1"


def test_login_bad_password(tmp_path):
    client = make_client(FakeED(), tmp_path, password="faux")
    with pytest.raises(EcoleDirecteError) as exc:
        run(client.connecter())
    assert exc.value.code == 505


def test_qcm_flow_and_persistence(tmp_path):
    fake = FakeED(qcm=True)
    client = make_client(fake, tmp_path)
    with pytest.raises(QCMRequired) as exc:
        run(client.connecter())
    assert exc.value.question == "Quelle est votre année de naissance ?"
    assert exc.value.propositions == ["2007", "2008", "2009"]

    with pytest.raises(QCMRequired):
        run(client.repondre_qcm("1999"))  # proposition inexistante

    compte = run(client.repondre_qcm("2"))
    assert compte.id == 1234
    assert json.loads((tmp_path / "etat.json").read_text())["eleve"]["fa"] == [{"cn": "CN", "cv": "CV"}]

    # Un nouveau client réutilise cn/cv : plus de QCM.
    client2 = make_client(fake, tmp_path)
    assert run(client2.connecter()).id == 1234


def test_token_renewed_on_expiry(tmp_path):
    fake = FakeED()
    client = make_client(fake, tmp_path)
    run(client.connecter())
    fake.expire_next = True
    data = run(client.requete("/v3/eleves/1234/notes.awp", data={"anneeScolaire": ""}))
    assert data["notes"]
    assert client.token == "tok2"


# ------------------------------------------------------------------ tools


@pytest.fixture
def fake_server(tmp_path):
    fake = FakeED()
    server.set_client(make_client(fake, tmp_path))
    yield fake
    server.set_client(None)


def test_tools_are_listed():
    noms = {t.name for t in run(server.mcp.list_tools())}
    assert {"se_connecter", "repondre_qcm", "notes", "devoirs", "emploi_du_temps", "vie_scolaire",
            "messages", "lire_message", "documents", "telecharger_fichier", "marquer_devoir"} <= noms
    notes_tool = next(t for t in run(server.mcp.list_tools()) if t.name == "notes")
    assert "matiere" in notes_tool.input_schema["properties"]


def test_notes_tool(fake_server):
    res = call_tool("notes", {})
    assert res["periode"] == "1er Trimestre"
    maths = next(d for d in res["disciplines"] if d["code"] == "MATHS")
    # (15/20*20*2 + 8/10*20*1) / 3 = 15.33
    assert maths["moyenne_calculee"] == "15,33"
    assert res["notes"][0]["devoir"] == "Absent"  # tri par date décroissante

    res = call_tool("notes", {"matiere": "fran"})
    assert [n["devoir"] for n in res["notes"]] == ["Dissert"]


def test_devoirs_tool(fake_server):
    res = call_tool("devoirs", {"date_jour": "2000-01-03"})
    item = res["jours"][0]["matieres"][0]
    assert item["a_faire"]["contenu"] == "Exercices 1 à 3"
    assert item["a_faire"]["pieces_jointes"][0]["id"] == 77

    res = call_tool("devoirs", {})
    assert res["jours"][0]["date"] == "2000-01-03"


def test_marquer_devoir(fake_server):
    result = run(server.mcp.call_tool("marquer_devoir", {"id_devoir": 9}))
    assert "fait" in result.content[0].text
    path, verbe, data = fake_server.calls[-1]
    assert verbe == "put" and data == {"idDevoirsEffectues": [9], "idDevoirsNonEffectues": []}


def test_emploi_du_temps_sorted(fake_server):
    res = call_tool("emploi_du_temps", {"date_debut": "2026-10-09"})
    cours = res["jours"][0]["cours"]
    assert [c["matiere"] for c in cours] == ["NSI", "ANGLAIS"]
    assert cours[1]["annule"] is True


def test_unknown_student_is_tool_error(fake_server):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="introuvable"):
        run(server.mcp.call_tool("notes", {"eleve": "Paul"}))
