"""
OpenBrowser-AI — Monitoring fonctionnel.
STRATÉGIE : OpenRouter (confirmé) -> Groq -> Google (dernier recours)
Vidéo native via CDP Page.startScreencast + imageio_ffmpeg (MP4 direct).
Preflight + succès AVANT erreurs fatales.
Logging DEBUG activé pour diagnostiquer le pipeline vidéo.

Corrections appliquées après lecture directe du code source openbrowser-ai :
  1. CodeAgent n'a PAS de paramètre `browser_profile` → passer via `browser=BrowserSession(...)`
  2. CodeAgent n'a PAS de paramètre pour system prompt custom → fusionné dans `task`
  3. CodeAgent n'appelle `browser_session.start()` QUE s'il crée lui-même la session
     (browser_session is None) → puisqu'on passe une session déjà construite via
     `browser=`, il faut l'appeler nous-mêmes AVANT de la passer, sinon toute
     action browser plante (CDP jamais initialisé).
  4. Vidéo trop courte (6 s pour un run de 205 s) : Page.startScreencast n'envoie
     une frame QUE quand la page se repeint, donc aucune frame pendant les attentes
     LLM, et le recorder écrit à 30 fps fixes → monkeypatch de VideoRecorderService
     (voir section dédiée plus bas).
  5. Run réel usinformatique.com -> ERROR : un appel à openrouter/free est resté
     muet 232 s (ChatOpenAI par défaut = timeout 600 s + 5 retries) jusqu'au
     timeout global. -> timeout par appel LLM (LLM_CALL_TIMEOUT_SECONDS).
  6. Erreur 'AIMessage' object has no attribute 'completion' (Gemini) : nos
     wrappers maison renvoyaient un AIMessage alors qu'openbrowser-ai attend un
     ChatInvokeCompletion. -> tous les providers passent par ChatOpenAI
     (endpoints compatibles OpenAI), wrappers supprimés.
  7. IDs Groq morts (llama-3.3-70b-specdec, qwen/qwen-3.5-32b) remplacés par
     ceux que Groq recommande sur sa page de dépréciation.
  8. Verdict fabriqué : sans JSON exploitable, le script déduisait UP/DOWN en
     cherchant "error" dans str(result) — or repr(CodeCell) contient toujours
     "error=None" -> DOWN systématique, sans aucun test réel. Supprimé : le
     rapport doit venir de done() (agent.namespace['_task_result']), et un UP
     sans interaction réelle (click/input/scroll...) est rejeté.
"""
import asyncio
import glob
import json
import logging
import os
import re
import threading
import time

# ⭐ ACTIVATION LOGS INTERNES DU FRAMEWORK (avant tout import openbrowser)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from openai import AsyncOpenAI
from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile, BrowserSession
from openbrowser.browser.video_recorder import VideoRecorderService
from openbrowser.browser.watchdogs.recording_watchdog import RecordingWatchdog


# ---------------------------------------------------------------------------
# ⭐ MONKEYPATCH VIDEO : combler les trous pendant les attentes LLM
#
# Problème confirmé sur un vrai run : 205 s réelles -> 199 frames -> 6,6 s de
# vidéo. CDP Page.startScreencast n'émet une frame que sur repaint visuel, donc
# pendant qu'un LLM lent réfléchit (page figée) il n'arrive aucune frame, et
# VideoRecorderService écrit à 30 fps fixes sans tenir compte du temps réel.
#
# Solution : quand une nouvelle frame arrive après une pause > MIN_GAP_SECONDS,
# on réécrit d'abord la dernière image (gel) pendant min(pause, cap) secondes.
#
# Pourquoi pas un simple "rappeler add_frame N fois" :
#   - add_frame() est appelé depuis un thread pool (run_in_executor) → il faut
#     un verrou, sinon état partagé corrompu + "generator already executing"
#   - add_frame() lance un sous-processus ffmpeg PAR frame → on réutilise le
#     tableau numpy déjà décodé et on l'écrit directement dans le writer
#   - le traitement d'une frame (ffmpeg) est plus lent que leur cadence d'arrivée
#     sur un runner CPU : il faut mesurer les pauses à l'instant de RÉCEPTION de
#     la frame (event loop, via on_screencastFrame), pas au moment où un thread
#     la traite, sinon le retard de traitement serait pris pour un silence.
#
# Réglage : VIDEO_FREEZE_CAP_SECONDS (env). 3 s ≈ vidéo compacte (~19 s pour
# le run de 205 s), 10 s ≈ plus proche du temps réel (~40 s).
# ---------------------------------------------------------------------------
FREEZE_CAP_SECONDS = float(os.environ.get("VIDEO_FREEZE_CAP_SECONDS", "3.0"))
MIN_GAP_SECONDS = 0.5  # en dessous : flux normal, on ne touche à rien

_video_lock = threading.Lock()
_orig_recorder_start = VideoRecorderService.start
_orig_recorder_add_frame = VideoRecorderService.add_frame


def _patched_recorder_start(self):
    _orig_recorder_start(self)
    self._last_array = None
    self._last_wall = None
    writer = self._writer
    if writer is not None:
        raw_append = writer.append_data

        def append_and_remember(arr, *args, **kwargs):
            self._last_array = arr
            return raw_append(arr, *args, **kwargs)

        writer.append_data = append_and_remember
        self._raw_append = raw_append


def _patched_recorder_add_frame(self, frame_data_b64, received_at=None):
    # received_at : instant de réception (fourni par on_screencastFrame patché).
    # Absent pour la capture finale de BrowserStopEvent -> on prend "maintenant".
    now = received_at if received_at is not None else time.monotonic()
    with _video_lock:  # add_frame arrive depuis un thread pool : on sérialise
        last = getattr(self, "_last_array", None)
        last_t = getattr(self, "_last_wall", None)
        if (
            self._is_active
            and self._writer is not None
            and last is not None
            and last_t is not None
        ):
            gap = now - last_t
            if gap > MIN_GAP_SECONDS:
                n_fill = int(min(gap, FREEZE_CAP_SECONDS) * self.framerate)
                for _ in range(n_fill):
                    # pas de ffmpeg : on réécrit l'image déjà décodée
                    self._raw_append(last)
        _orig_recorder_add_frame(self, frame_data_b64)
        # max() : les threads peuvent passer le verrou dans le désordre
        self._last_wall = now if last_t is None else max(now, last_t)


def _patched_on_screencast_frame(self, event, session_id):
    """Copie de RecordingWatchdog.on_screencastFrame (openbrowser-ai 0.1.50)
    + horodatage de réception transmis à add_frame."""
    if not self._recorder:
        return
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None, self._recorder.add_frame, event["data"], time.monotonic()
    )
    asyncio.create_task(self._ack_screencast_frame(event, session_id))


VideoRecorderService.start = _patched_recorder_start
VideoRecorderService.add_frame = _patched_recorder_add_frame
RecordingWatchdog.on_screencastFrame = _patched_on_screencast_frame


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "")
SITE_ID = os.environ.get("SITE_ID", "")
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ.get("REQUIREMENTS", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Groq key          : " + str(bool(GROQ_API_KEY)))
print("Google AI Std key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key    : " + str(bool(OPENROUTER_API_KEY)))
print("Video freeze cap  : %.1fs" % FREEZE_CAP_SECONDS)


# ---------------------------------------------------------------------------
# Construction du LLM — ChatOpenAI pour TOUS les providers
# (OpenRouter, Groq et Google AI Studio exposent des endpoints compatibles
# OpenAI ; ChatOpenAI renvoie le ChatInvokeCompletion qu'attend openbrowser-ai)
# ---------------------------------------------------------------------------
def build_llm(model_config):
    llm_kwargs = {}
    if model_config["provider"] != "openrouter":
        # openrouter/free : réglages historiques inchangés (validés en vrai).
        # Groq / Google : on n'envoie ni frequency_penalty (0.3 par défaut dans
        # ChatOpenAI) ni plafond de tokens, pour rester sur les défauts du provider.
        llm_kwargs = {"frequency_penalty": None, "max_completion_tokens": None}
    return ChatOpenAI(
        model=model_config["model"],
        base_url=model_config["base_url"],
        api_key=model_config["key"],
        temperature=0.0,
        # Par défaut ChatOpenAI = timeout lecture 600 s + 5 retries : un appel
        # muet bloquait tout le run. Ici l'appel échoue vite et la boucle
        # interne d'openbrowser-ai le relance (compteur "consecutive errors").
        timeout=LLM_CALL_TIMEOUT,
        max_retries=0,
        **llm_kwargs,
    )


# ---------------------------------------------------------------------------
# Chaîne de modèles — OpenRouter d'abord
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

if OPENROUTER_API_KEY:
    MODEL_CHAIN.append({
        "provider": "openrouter",
        "model": "openrouter/free",
        "key": OPENROUTER_API_KEY,
        "base_url": "https://openrouter.ai/api/v1",
    })

# IDs surchargeables par variable d'environnement (séparés par des virgules) :
# les catalogues changent souvent (Groq a déjà retiré 4 modèles en 2026).
# Défaut Groq = remplacements recommandés par Groq (console.groq.com/docs/deprecations)
GROQ_MODELS = os.environ.get("GROQ_MODELS", "openai/gpt-oss-120b,qwen/qwen3.6-27b")
GOOGLE_MODELS = os.environ.get("GOOGLE_MODELS", "gemini-3.5-flash,gemini-3.6-flash")

if GROQ_API_KEY:
    for m in [x.strip() for x in GROQ_MODELS.split(",") if x.strip()]:
        MODEL_CHAIN.append({
            "provider": "groq",
            "model": m,
            "key": GROQ_API_KEY,
            "base_url": "https://api.groq.com/openai/v1",
        })

if GOOGLE_API_KEY:
    for m in [x.strip() for x in GOOGLE_MODELS.split(",") if x.strip()]:
        MODEL_CHAIN.append({
            "provider": "google_openai",
            "model": m,
            "key": GOOGLE_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        })

if not MODEL_CHAIN:
    print("ERREUR : aucune cle API disponible. Abandon.")
    import sys
    sys.exit(1)

print("Chaine finale :")
for i, entry in enumerate(MODEL_CHAIN):
    print("  %d. [%s] %s" % (i + 1, entry["provider"], entry["model"]))

# Un vrai run a montré des appels LLM légitimes de 68 s et 76 s sur openrouter/free
# -> le timeout par appel doit rester au-dessus, et le global aussi.
LLM_CALL_TIMEOUT = float(os.environ.get("LLM_CALL_TIMEOUT_SECONDS", "100"))
GLOBAL_TIMEOUT_SECONDS = float(os.environ.get("GLOBAL_TIMEOUT_SECONDS", "300"))
MAX_STEPS = 8
DELAY_BETWEEN_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Template de consignes (fusionné dans task, pas dans system_prompt.md
# puisque CodeAgent n'a aucun point d'injection pour un system prompt custom)
# ---------------------------------------------------------------------------
CONSIGNES_TEMPLATE = """
Tu es un ingenieur QA fonctionnel. TESTE l'application web, ne te contente pas d'observer.

FORMAT DE SORTIE (CRITIQUE) :
Tu DOIS ecrire des blocs Python entre triple backticks. Exemple :

Je verifie le titre de la page.
```python
title = await evaluate('document.title')
print(title)
```

N'utilise PAS de tags XML. N'utilise PAS la syntaxe tool_name(args).
UNIQUEMENT des blocs de code Python.

REGLES :
1. Effectue au moins UNE vraie interaction utilisateur et verifie avec une assertion.
2. Page chargee N'EST PAS un test. Bouton existe N'EST PAS un test.
3. Test valide = action + observation + assertion.
4. Utilise Python assert.

CONDITION D'ARRET STRICTE :
- Apres 6 appels d'outils maximum, appelle done() quoi qu'il arrive.
- Les resultats partiels sont acceptables.

ANOMALIES (echecs fonctionnels) :
- Texte contenant Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.
- Lien avec /undefined dans l'URL.
- Liste de produits vide sur une page catalogue.

INTERACTIONS REQUISES :
__REQUIREMENTS__

SORTIE FINALE :
Appelle done(text=...) avec UNIQUEMENT un JSON valide dans text :
  overall_status, site_type, actions_completed, model_used, pages[].

Exemple de forme attendue (remplace les valeurs par le resultat REEL observe) :
```python
import json
result = {"overall_status": "DOWN ou UP selon ce que tu observes",
"site_type": "__SITE_TYPE__", "actions_completed": True,
"model_used": "__MODEL_NAME__",
"pages": [{"url": "page testee", "status": "DOWN ou UP",
"http_code": 200, "action_tested": "description de l'action",
"assertion_passed": True_ou_False, "note": "ce que tu as observe"}]}
await done(text=json.dumps(result, ensure_ascii=False), success=True)
```
"""


def build_full_task(model_name, basic_task):
    """
    Construit la tache complete : URL + instructions de base, puis les
    consignes QA completes (format JSON, regles, limite d'appels).

    IMPORTANT : CodeAgent n'a aucun parametre pour un system prompt custom
    (system_prompt.md est fixe en interne, charge tel quel). Tout doit donc
    passer par `task`, qui devient un simple UserMessage("Task: " + task).
    extract_url_from_task() scanne l'integralite de la chaine (pas seulement
    le debut) et deduplique via set(), donc repeter SITE_URL plus loin dans
    les consignes ne casse pas la detection automatique d'URL.
    """
    consignes = (
        CONSIGNES_TEMPLATE
        .replace("__REQUIREMENTS__", REQUIREMENTS)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
    )
    return basic_task + "\n\n---\n\n" + consignes


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def extract_json_from_text(text):
    if not text:
        return None
    match = re.search(r"{.*}", text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            cleaned = re.sub(r"(?<!\\)\n", " ", candidate)
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def extract_final_result(result):
    cells = getattr(result, "cells", None) or getattr(result, "history", None) or []
    for cell in reversed(list(cells)):
        source = getattr(cell, "source", "") or ""
        if "done(" not in source:
            continue
        match = re.search(
            r"done\s*\(\s*text\s*=\s*['\"](.+?)['\"]\s*,\s*success",
            source, re.DOTALL,
        )
        if match:
            return match.group(1)
    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if '"url"' in output or '"overall_status"' in output:
            return output
    return ""  # surtout PAS str(result) : il contient toujours "error=None"


VALID_STATUSES = ("UP", "DOWN", "ERROR", "UNKNOWN")

_INTERACTION_RE = re.compile("|".join([
    r"\b(?:click|input_text|send_keys|select_dropdown|scroll|upload_file)\s*\(",
    r"\.(?:click|focus|submit)\s*\(",
    r"\bscroll(?:To|By|IntoView)\b",
    r"\bdispatchEvent\b",
]))


def parse_report(text):
    """JSON de rapport valide (overall_status reconnu) ou None. Aucune deduction."""
    if not text:
        return None
    candidate = None
    if isinstance(text, dict):
        candidate = text
    else:
        text = str(text).strip()
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            candidate = extract_json_from_text(text)
    if (
        isinstance(candidate, dict)
        and str(candidate.get("overall_status", "")).upper() in VALID_STATUSES
    ):
        return candidate
    return None


def get_agent_report(agent, result):
    """Source fiable = ce que l'agent a remis via done() : agent.namespace['_task_result']."""
    ns = getattr(agent, "namespace", None) or {}
    if ns.get("_task_done") and ns.get("_task_result"):
        report = parse_report(ns["_task_result"])
        if report is not None:
            return report
    # second recours : anciens chemins (source de cellule done(...) / sortie)
    return parse_report(extract_final_result(result))


def count_real_interactions(cells):
    """Cellules reussies contenant une vraie interaction (pas navigate/evaluate de lecture)."""
    n = 0
    for cell in cells:
        if getattr(cell, "error", None):
            continue
        source = getattr(cell, "source", "") or ""
        if "done(" in source:
            continue
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        if _INTERACTION_RE.search(code):
            n += 1
    return n


def normalize_report(report, model_name):
    status = str(report["overall_status"]).upper()
    report["overall_status"] = status
    report["model_used"] = model_name
    report.setdefault("site_type", SITE_TYPE)
    report.setdefault("actions_completed", True)
    if not isinstance(report.get("pages"), list) or not report["pages"]:
        report["pages"] = [{
            "url": SITE_URL,
            "status": status,
            "http_code": None,
            "action_tested": None,
            "assertion_passed": status == "UP",
            "note": "Rapport sans detail par page.",
        }]
    return report


def judge_run(agent, result, model_name):
    """(rapport | None, raison). Le verdict vient de l'agent, jamais d'une heuristique."""
    cells = list(getattr(result, "cells", None) or [])
    report = get_agent_report(agent, result)
    interactions = count_real_interactions(cells)
    if report is None:
        return None, "aucun rapport JSON remis par done() : l'agent n'a pas conclu"
    if str(report["overall_status"]).upper() == "UP" and interactions == 0:
        return None, "UP annonce sans aucune interaction reelle (navigate/evaluate seuls) : pas un test"
    return normalize_report(report, model_name), "%d interaction(s) reelle(s)" % interactions


# ---------------------------------------------------------------------------
# Fermeture session (le flush video se fait via BrowserStopEvent interne,
# declenche par stop())
# ---------------------------------------------------------------------------
async def close_agent_session(agent):
    if agent is None:
        return

    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
            print("Session trouvee via agent." + attr_name)
            break

    if session is None:
        print("Aucune session trouvee sur l'agent")
    else:
        # stop() declenche BrowserStopEvent -> le RecordingWatchdog interne
        # finalise automatiquement le fichier MP4 via CDP+ffmpeg
        for method_name in ("close", "stop", "kill", "shutdown", "cleanup"):
            if not hasattr(session, method_name):
                continue
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    await r
                print("Session fermee via " + method_name + "()")
                break
            except Exception as e:
                print("Echec " + method_name + "() : " + str(e))

    # Laisser le pipeline CDP+ffmpeg ecrire le MP4 final sur disque
    await asyncio.sleep(5)

    videos = (
        glob.glob(os.path.join(RECORDINGS_DIR, "*.mp4"))
        + glob.glob(os.path.join(RECORDINGS_DIR, "*.webm"))
    )
    print("Videos trouvees dans %s : %d" % (RECORDINGS_DIR, len(videos)))
    for v in videos:
        print("  -> " + v + " (" + str(os.path.getsize(v)) + " octets)")
    if not videos:
        print("Contenu du dossier : " + str(os.listdir(RECORDINGS_DIR)))


# ---------------------------------------------------------------------------
# Detection erreurs (appelees UNIQUEMENT si pas de resultat exploitable)
# ---------------------------------------------------------------------------
def is_fatal_model_error(output_text):
    lower = output_text.lower()
    return any(p in lower for p in [
        "agentic harness",
        "only available on agentic",
        "8 consecutive llm failures",
        "terminating: 8 consecutive",
        "no endpoints found",
        "this model is unavailable",
        "model not found",
        "is no longer available",
        "unavailable for free",
        "authenticationerror",
        "invalid_api_key",
        "does not exist",
        "not found for account",
    ])


def is_quota_error(output_text):
    lower = output_text.lower()
    return any(p in lower for p in [
        "quota exceeded",
        "resource_exhausted",
        "429",
        "too many requests",
        "free_tier_requests",
        "please retry in",
        "insufficient balance",
        "insufficient_balance",
        "balance is not enough",
        "402",
    ])


# ---------------------------------------------------------------------------
# Preflight API (1 token)
# ---------------------------------------------------------------------------
async def preflight_api_check(model_config):
    try:
        client = AsyncOpenAI(
            api_key=model_config["key"],
            base_url=model_config["base_url"],
        )
        await asyncio.wait_for(
            client.chat.completions.create(
                model=model_config["model"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            ),
            timeout=20,
        )
        print("Preflight OK : %s" % model_config["model"])
        return True
    except Exception as e:
        print("Preflight ECHEC : %s -> %s" % (
            model_config["model"], str(e)[:200]))
        return False


# ---------------------------------------------------------------------------
# Tentative
#   FIX #1 : succes AVANT erreurs fatales/quota
#   FIX #2 : browser=BrowserSession(...) au lieu de browser_profile=...
#   FIX #3 : consignes QA fusionnees dans task (pas de system prompt custom)
#   FIX #4 : await browser_session.start() AVANT de la passer a CodeAgent
#            (CodeAgent ne l'appelle que s'il cree lui-meme la session)
#   FIX #6 : ChatOpenAI pour tous les providers + timeout par appel LLM
#   FIX #7 : verdict = rapport de done() uniquement (plus d'heuristique par mots-cles)
#   FIX #5 : monkeypatch VideoRecorderService (gel des frames pendant les
#            attentes LLM, voir section MONKEYPATCH VIDEO en haut du fichier)
# ---------------------------------------------------------------------------
async def run_attempt(model_config, task):
    provider = model_config["provider"]
    model_name = model_config["model"]

    print("=" * 60)
    print("TENTATIVE - PROVIDER : %s | MODELE : %s" % (provider, model_name))
    print("=" * 60)

    agent = None
    browser_session = None
    start_time = time.time()

    try:
        llm = build_llm(model_config)

        # ⭐ FIX #2 : CodeAgent n'accepte PAS browser_profile en kwarg
        # (silencieusement ignore et logge dans "Ignoring additional kwargs").
        # Il faut construire et passer une vraie BrowserSession.
        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
            record_video_dir=RECORDINGS_DIR,
        )
        browser_session = BrowserSession(browser_profile=profile)

        # ⭐ FIX #4 : demarrage manuel obligatoire. CodeAgent.run() ne fait
        # `await browser_session.start()` QUE s'il cree lui-meme la session
        # (browser_session is None au depart). Comme on fournit une session
        # deja construite via `browser=`, il faut la demarrer nous-memes,
        # sinon toute action (navigate/evaluate/click) plante car le CDP
        # n'est jamais initialise.
        await browser_session.start()

        # ⭐ FIX #3 : CodeAgent n'a pas de parametre pour un system prompt
        # custom (system_prompt.md est fixe en interne, aucun point
        # d'injection). Les consignes QA sont fusionnees dans task.
        full_task = build_full_task(model_name, task)

        agent = CodeAgent(
            task=full_task,
            llm=llm,
            browser=browser_session,
            max_steps=MAX_STEPS,
        )

        try:
            result = await asyncio.wait_for(
                agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
            print("agent.run() termine en %.1fs" % (time.time() - start_time))
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        # --- ETAPE 1 : le verdict doit venir de done(), jamais d'une heuristique ---
        raw_output_text = str(result)
        cells = getattr(result, "cells", None) or []
        report, reason = judge_run(agent, result, model_name)
        print("Cellules executees : %d | %s" % (len(cells), reason))

        if report is not None:
            print("Rapport retenu (" + model_name + ") :")
            print(json.dumps(report, ensure_ascii=False)[:800])
            return report

        # --- ETAPE 2 : seulement si pas de resultat exploitable ---
        if is_fatal_model_error(raw_output_text):
            print("ERREUR FATALE : %s -> skip" % model_name)
            return None
        if is_quota_error(raw_output_text):
            print("QUOTA/SOLDE : %s -> skip" % model_name)
            return None

        print("REJETE : %s -> %s" % (model_name, reason))
        return None

    except Exception as err:
        print("Echec avec " + model_name + " : " + str(err))
        return None

    finally:
        await close_agent_session(agent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    task = (
        "Navigate to " + SITE_URL + ". "
        "Execute the required interactions. "
        "Use Python assertions. "
        "Detect anomalies (errors, undefined, empty listings). "
        "HARD LIMIT: 6 tool calls max, then call done(). "
        "IMPORTANT: write ONLY valid Python code blocks between triple backticks."
    )

    final_report = None
    total = len(MODEL_CHAIN)

    for idx, model_config in enumerate(MODEL_CHAIN):
        print("")
        print("#" * 60)
        print("# Essai %d/%d : [%s] %s" % (
            idx + 1, total, model_config["provider"], model_config["model"]))
        print("#" * 60)

        if not await preflight_api_check(model_config):
            print("Modele ecarte des le preflight : %s" % model_config["model"])
            continue

        report = await run_attempt(model_config, task)
        if report is not None:
            final_report = report
            print("Modele retenu : %s" % model_config["model"])
            break
        else:
            print("Modele ecarte : %s" % model_config["model"])
            if idx < total - 1:
                print("Attente %ds..." % DELAY_BETWEEN_ATTEMPTS)
                await asyncio.sleep(DELAY_BETWEEN_ATTEMPTS)

    if final_report is None:
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [{
                "url": SITE_URL,
                "status": "ERROR",
                "http_code": None,
                "action_tested": None,
                "assertion_passed": False,
                "note": "Tous les modeles ont echoue.",
            }],
            "error": "Tous les modeles ont echoue.",
        }

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("")
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
