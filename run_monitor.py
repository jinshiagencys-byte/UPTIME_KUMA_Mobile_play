"""
OpenBrowser-AI — Monitoring fonctionnel (Cloudflare Workers AI).

STRATÉGIE : Un seul provider, Cloudflare Workers AI (API compatible OpenAI v1,
https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1),
modèle par défaut @cf/zai-org/glm-4.7-flash.
"""
import asyncio
import inspect
import json
import logging
import os
import re
import sys
import threading
import time

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
# MONKEYPATCH VIDEO : combler les trous pendant les attentes LLM
# ---------------------------------------------------------------------------
FREEZE_CAP_SECONDS = float(os.environ.get("VIDEO_FREEZE_CAP_SECONDS", "3.0"))
MIN_GAP_SECONDS = 0.5

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
    now = received_at if received_at is not None else time.monotonic()
    with _video_lock:
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
                    self._raw_append(last)
        _orig_recorder_add_frame(self, frame_data_b64)
        self._last_wall = now if last_t is None else max(now, last_t)


def _patched_on_screencast_frame(self, event, session_id):
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
# Env / Réglages
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "")
SITE_ID = os.environ.get("SITE_ID", "")
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ.get("REQUIREMENTS", "")
PAGES_JSON = os.environ.get("PAGES_JSON", "")

# Cloudflare Workers AI
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_AUTH_TOKEN = os.environ.get("CLOUDFLARE_AUTH_TOKEN", "")
CLOUDFLARE_MODEL = os.environ.get("CLOUDFLARE_MODEL", "@cf/zai-org/glm-4.7-flash")
CLOUDFLARE_BASE_URL = os.environ.get(
    "CLOUDFLARE_BASE_URL",
    f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1"
)

DEAD_PROVIDERS = set()
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", "2"))

_VALID_EFFORTS = ("minimal", "low", "medium", "high")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low").strip().lower()
if REASONING_EFFORT in ("", "off", "none", "0"):
    REASONING_EFFORT = ""
elif REASONING_EFFORT not in _VALID_EFFORTS:
    print("REASONING_EFFORT invalide (%r) : désactivé" % REASONING_EFFORT)
    REASONING_EFFORT = ""

USE_VISION = os.environ.get("USE_VISION", "1") != "0"

VIEWPORT_WIDTH = int(os.environ.get("VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.environ.get("VIEWPORT_HEIGHT", "720"))

LLM_CALL_TIMEOUT = float(os.environ.get("LLM_CALL_TIMEOUT_SECONDS", "120"))
GLOBAL_TIMEOUT_SECONDS = float(os.environ.get("GLOBAL_TIMEOUT_SECONDS", "420"))
TOTAL_BUDGET_SECONDS = float(os.environ.get("TOTAL_BUDGET_SECONDS", "900"))
MIN_ATTEMPT_SECONDS = 120
DEADLINE_MARGIN_SECONDS = 20

MAX_STEPS = int(os.environ.get("MAX_STEPS", "14"))
TEST_STEPS = max(MAX_STEPS - 4, 4)
DONE_STEP = TEST_STEPS + 1
DELAY_BETWEEN_ATTEMPTS = 3

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

try:
    _CODEAGENT_PARAMS = set(inspect.signature(CodeAgent.__init__).parameters)
except (TypeError, ValueError):
    _CODEAGENT_PARAMS = set()

print("Recordings dir       : " + RECORDINGS_DIR)
print("Cloudflare Account ID: " + CLOUDFLARE_ACCOUNT_ID)
print("Cloudflare Auth Token: " + str(bool(CLOUDFLARE_AUTH_TOKEN)))
print("Cloudflare Modèle    : " + CLOUDFLARE_MODEL)
print("Video freeze cap     : %.1fs" % FREEZE_CAP_SECONDS)
print("Viewport             : %dx%d" % (VIEWPORT_WIDTH, VIEWPORT_HEIGHT))
print("Vision (captures)    : " + ("ON" if USE_VISION else "OFF"))
print("Max steps / essai    : %d | pages max / run : %d" % (MAX_STEPS, MAX_PAGES_PER_RUN))


# ---------------------------------------------------------------------------
# LLM — ChatOpenAI
# ---------------------------------------------------------------------------
class ReasoningChatOpenAI(ChatOpenAI):
    def get_client(self):
        client = super().get_client()
        effort = getattr(self, "_or_reasoning", None)
        if effort:
            completions = client.chat.completions
            orig_create = completions.create

            async def create_with_reasoning(*args, **kwargs):
                extra = dict(kwargs.get("extra_body") or {})
                extra.setdefault("reasoning", {"effort": effort})
                kwargs["extra_body"] = extra
                return await orig_create(*args, **kwargs)

            completions.create = create_with_reasoning
        return client


_reasoning_disabled = set()


def reasoning_for(model_config):
    if model_config["provider"] != "openrouter" or not REASONING_EFFORT:
        return None
    if (model_config["provider"], model_config["model"]) in _reasoning_disabled:
        return None
    return REASONING_EFFORT


def build_llm(model_config):
    llm_kwargs = {}
    if model_config["provider"] not in ("openrouter", "unorouter", "cloudflare"):
        llm_kwargs = {"frequency_penalty": None, "max_completion_tokens": None}
    llm = ReasoningChatOpenAI(
        model=model_config["model"],
        base_url=model_config["base_url"],
        api_key=model_config["key"],
        temperature=0.0,
        timeout=LLM_CALL_TIMEOUT,
        max_retries=0,
        **llm_kwargs,
    )
    llm._or_reasoning = reasoning_for(model_config)
    return llm


# ---------------------------------------------------------------------------
# Chaîne de modèles : Cloudflare Workers AI
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

if CLOUDFLARE_AUTH_TOKEN and CLOUDFLARE_ACCOUNT_ID:
    MODEL_CHAIN.append({
        "provider": "cloudflare",
        "model": CLOUDFLARE_MODEL,
        "key": CLOUDFLARE_AUTH_TOKEN,
        "base_url": CLOUDFLARE_BASE_URL,
    })

if not MODEL_CHAIN:
    print("ERREUR : Configuration Cloudflare incomplète (ACCOUNT_ID ou AUTH_TOKEN manquant). Abandon.")
    sys.exit(1)

print("Chaîne finale :")
for i, entry in enumerate(MODEL_CHAIN):
    print("  %d. [%s] %s" % (i + 1, entry["provider"], entry["model"]))


# ---------------------------------------------------------------------------
# Consignes
# ---------------------------------------------------------------------------
CONSIGNES_TEMPLATE = """
Tu es un ingenieur QA fonctionnel. TESTE l'application web, ne te contente pas d'observer.

FORMAT DE SORTIE (CRITIQUE : une reponse hors format fait PERDRE une etape) :
- Chaque reponse = UNE phrase courte, puis UN SEUL bloc ```python.
- JAMAIS de tags <tool_call>, <arg_key>, <arg_value>. JAMAIS de nom_outil(args) hors bloc python.
- UNE seule petite action par etape (navigate, OU input_text puis click, OU un scroll).
- N'ecris JAMAIS await done() dans la meme reponse que tes tests. Attends d'avoir
  VU le resultat de tes actions (etape suivante) avant de conclure.
- N'invente PAS de selecteurs CSS : utilise les index des elements affiches dans
  l'etat de la page (click(index=N), input_text(index=N, text=...)).

Exemple de reponse valide :
Je verifie le titre de la page.
```python
title = await evaluate('document.title')
print(title)
```

BUDGET : **MAX_STEPS** etapes au total.

* Etapes 1 a **TEST_STEPS** : tester.
* A partir de l'etape **DONE_STEP** : appelle done() avec ce que tu as observe, meme incomplet.
* Un rapport incomplet mais honnete vaut mieux que pas de rapport.

REGLES ANTI-BLOCAGE (CRITIQUES) :

* Verifie les INTERACTIONS REQUISES l'une apres l'autre, dans l'ordre.
* MAXIMUM 2 TENTATIVES par interaction. Si elle echoue, passe a la suivante.
* Verifie le CONTENU affiche, pas seulement l'URL.

PAGE A TESTER : **TARGET_URL**
INTERACTIONS REQUISES :
**REQUIREMENTS**

SORTIE FINALE :
Appelle done(text=...) avec UNIQUEMENT un JSON valide dans text :
overall_status (UP ou DOWN), site_type, actions_completed, model_used, pages[].

```python
import json
result = {"overall_status": "DOWN ou UP selon ce que tu as observe",
"site_type": "__SITE_TYPE__", "actions_completed": True,
"model_used": "__MODEL_NAME__",
"pages": [{"url": "__TARGET_URL__", "status": "DOWN ou UP",
"http_code": 200, "action_tested": "description de l'action",
"assertion_passed": True, "note": "ce que tu as observe"}]}
await done(text=json.dumps(result, ensure_ascii=False), success=True)
```
"""

PAGE_REQUIREMENTS = (
    "1. Assert the page content loaded (title and main content are not an error). "
    "2. Perform ONE real interaction and assert the result is correct."
)


def build_full_task(model_name, target_url, requirements):
    basic_task = (
        "Navigate to " + target_url + ". "
        "Execute the required interactions. Use Python assertions. "
        "IMPORTANT: write ONLY valid Python code blocks between triple backticks."
    )
    consignes = (
        CONSIGNES_TEMPLATE
        .replace("**REQUIREMENTS**", requirements)
        .replace("**SITE_TYPE**", SITE_TYPE)
        .replace("**MODEL_NAME**", model_name)
        .replace("**TARGET_URL**", target_url)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
        .replace("__TARGET_URL__", target_url)
        .replace("**MAX_STEPS**", str(MAX_STEPS))
        .replace("**TEST_STEPS**", str(TEST_STEPS))
        .replace("**DONE_STEP**", str(DONE_STEP))
    )
    return basic_task + "\n\n---\n\n" + consignes


# ---------------------------------------------------------------------------
# Traitement des rapports & résultats
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
            r"done\s*\(\s*text\s*=\s*['\"](.*?)['\"]\s*,\s*success",
            source, re.DOTALL,
        )
        if match:
            return match.group(1)
    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if '"url"' in output or '"overall_status"' in output:
            return output
    return ""


VALID_STATUSES = ("UP", "DOWN", "ERROR", "UNKNOWN")

_INTERACTION_RE = re.compile("|".join([
    r"\b(?:click|input_text|send_keys|select_dropdown|scroll|upload_file)\s*\(",
    r"\.(?:click|focus|submit)\s*\(",
    r"\bscroll(?:To|By|IntoView)\b",
    r"\bdispatchEvent\b",
]))


def parse_report(text):
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
    ns = getattr(agent, "namespace", None) or {}
    if ns.get("_task_done") and ns.get("_task_result"):
        report = parse_report(ns["_task_result"])
        if report is not None:
            return report
    return parse_report(extract_final_result(result))


def count_real_interactions(cells):
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


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "oui", "yes")
    return value is True


def _is_false(value):
    if isinstance(value, str):
        return value.strip().lower() in ("false", "0", "non", "no")
    return value is False


def normalize_report(report, model_name, target_url):
    status = str(report["overall_status"]).upper()
    pages = [p for p in (report.get("pages") or []) if isinstance(p, dict)]

    failed = [p for p in pages if _is_false(p.get("assertion_passed"))]
    if status == "UP" and failed:
        status = "DOWN"
        first = failed[0]
        first["note"] = (str(first.get("note") or "").strip()
                         + " | Verdict corrigé UP -> DOWN : assertion échouée.").strip(" |")

    report["overall_status"] = status
    report["model_used"] = model_name
    report.setdefault("site_type", SITE_TYPE)
    report.setdefault("actions_completed", True)
    if not pages:
        report["pages"] = [{
            "url": target_url,
            "status": status,
            "http_code": None,
            "action_tested": None,
            "assertion_passed": status == "UP",
            "note": "Rapport sans détail par page.",
        }]
    return report


def judge_run(agent, result, model_name, target_url):
    cells = list(getattr(result, "cells", None) or [])
    report = get_agent_report(agent, result)
    interactions = count_real_interactions(cells)
    
    if report is None:
        return None, "aucun rapport JSON remis par done()"
    if str(report["overall_status"]).upper() == "UP" and interactions == 0:
        return None, "UP annoncé sans interaction réelle"
    
    return normalize_report(report, model_name, target_url), "%d interaction(s) réelle(s)" % interactions


def collapse_to_page(report, target_url):
    pages = [p for p in (report.get("pages") or []) if isinstance(p, dict)]
    status = str(report["overall_status"]).upper()
    actions = [str(p["action_tested"]) for p in pages if p.get("action_tested")]
    notes = [str(p["note"]) for p in pages if p.get("note")]
    codes = [p["http_code"] for p in pages if p.get("http_code")]
    
    if pages:
        passed = all(_as_bool(p.get("assertion_passed")) for p in pages)
    else:
        passed = status == "UP"
        
    return {
        "url": target_url,
        "status": status,
        "http_code": codes[0] if codes else None,
        "action_tested": (" ; ".join(actions))[:300] or None,
        "assertion_passed": passed,
        "note": (" | ".join(notes))[:400],
    }


def build_partial_report(cells, model_name, target_url):
    interactions = count_real_interactions(cells)
    if interactions == 0:
        return None, 0
    obs = []
    for cell in cells[-4:]:
        err = getattr(cell, "error", None)
        out = (getattr(cell, "output", "") or "").strip()
        if err:
            obs.append("erreur: " + str(err)[:150])
        elif out:
            obs.append(out[:150])
            
    note = "Test incomplet. %d interaction(s) exécutée(s)." % interactions
    if obs:
        note += " Dernières observations : " + " / ".join(obs)
        
    return {
        "url": target_url,
        "status": "ERROR",
        "http_code": None,
        "action_tested": "%d interaction(s) sans conclusion (%s)" % (interactions, model_name),
        "assertion_passed": False,
        "note": note[:500],
    }, interactions


async def close_agent_session(agent):
    if agent is None:
        return
    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
            break
            
    if session is not None:
        for method_name in ("close", "stop", "kill", "shutdown", "cleanup"):
            if not hasattr(session, method_name):
                continue
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    await r
                break
            except Exception:
                pass
    await asyncio.sleep(2)


def is_fatal_model_error(text):
    lower = text.lower()
    return any(p in lower for p in [
        "authenticationerror",
        "invalid_api_key",
        "10000",
        "unauthorized",
    ])


def is_quota_error(text):
    lower = text.lower()
    return any(p in lower for p in [
        "rate limit",
        "quota exceeded",
        "too many requests",
    ])


_preflight_cache = {}


async def _ping(model_config, effort=None):
    client = AsyncOpenAI(
        api_key=model_config["key"],
        base_url=model_config["base_url"],
    )
    kwargs = {
        "model": model_config["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=20)


async def preflight_api_check(model_config):
    key = (model_config["provider"], model_config["model"])
    if key in _preflight_cache:
        return _preflight_cache[key]
    ok = False
    try:
        await _ping(model_config)
        print("Preflight OK : %s" % model_config["model"])
        ok = True
    except Exception as e:
        print("Preflight ÉCHEC : %s -> %s" % (model_config["model"], str(e)[:200]))
    _preflight_cache[key] = ok
    return ok


# ---------------------------------------------------------------------------
# Exécution des essais
# ---------------------------------------------------------------------------

async def run_attempt(model_config, target_url, requirements, deadline):
    provider = model_config["provider"]
    model_name = model_config["model"]

    print("=" * 60)
    print("TENTATIVE - PROVIDER : %s | MODÈLE : %s | PAGE : %s" % (
        provider, model_name, target_url))
    print("=" * 60)

    outcome = {"report": None, "partial": (None, 0), "stop": False}
    agent = None
    browser_session = None
    start_time = time.time()

    try:
        llm = build_llm(model_config)
        profile = BrowserProfile(
            headless=True,
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            record_video_dir=RECORDINGS_DIR,
        )
        browser_session = BrowserSession(browser_profile=profile)
        await browser_session.start()

        full_task = build_full_task(model_name, target_url, requirements)
        agent_kwargs = {}
        if "use_vision" in _CODEAGENT_PARAMS:
            agent_kwargs["use_vision"] = USE_VISION
            
        agent = CodeAgent(
            task=full_task,
            llm=llm,
            browser=browser_session,
            max_steps=MAX_STEPS,
            **agent_kwargs,
        )

        attempt_timeout = min(
            GLOBAL_TIMEOUT_SECONDS,
            deadline - time.monotonic() - DEADLINE_MARGIN_SECONDS,
        )
        if attempt_timeout < 60:
            print("Temps restant insuffisant : essai abandonné")
            return outcome

        try:
            result = await asyncio.wait_for(agent.run(), timeout=attempt_timeout)
            print("agent.run() terminé en %.1fs" % (time.time() - start_time))
        except asyncio.TimeoutError:
            print("Timeout pour %s" % model_name)
            cells = list(getattr(getattr(agent, "session", None), "cells", None) or [])
            outcome["partial"] = build_partial_report(cells, model_name, target_url)
            return outcome

        cells = list(getattr(result, "cells", None) or [])
        report, reason = judge_run(agent, result, model_name, target_url)

        if report is not None:
            outcome["report"] = report
            return outcome

        outcome["partial"] = build_partial_report(cells, model_name, target_url)
        raw = str(result)
        if is_fatal_model_error(raw) or is_quota_error(raw):
            outcome["stop"] = True
        return outcome

    except Exception as err:
        text = str(err)
        print("Échec avec " + model_name + " : " + text)
        if is_fatal_model_error(text) or is_quota_error(text):
            outcome["stop"] = True
        return outcome

    finally:
        await close_agent_session(agent)


async def test_page(target_url, requirements, deadline):
    best_partial = (None, 0)
    for idx, model_config in enumerate(MODEL_CHAIN):
        dead_key = (model_config["provider"], model_config["model"])
        if dead_key in DEAD_PROVIDERS:
            continue
        if time.monotonic() > deadline - MIN_ATTEMPT_SECONDS:
            break

        if not await preflight_api_check(model_config):
            continue

        outcome = await run_attempt(model_config, target_url, requirements, deadline)
        if outcome["report"] is not None:
            return outcome["report"], best_partial, False
        if outcome["partial"][1] > best_partial[1]:
            best_partial = outcome["partial"]
        if outcome["stop"]:
            DEAD_PROVIDERS.add(dead_key)
            continue
        if idx < len(MODEL_CHAIN) - 1:
            await asyncio.sleep(DELAY_BETWEEN_ATTEMPTS)

    all_dead = all((mc["provider"], mc["model"]) in DEAD_PROVIDERS for mc in MODEL_CHAIN)
    return None, best_partial, all_dead


def load_other_pages():
    others, seen = [], {SITE_URL.rstrip("/")}
    if not PAGES_JSON.strip():
        return others
    try:
        data = json.loads(PAGES_JSON)
    except json.JSONDecodeError:
        return others
    for p in data or []:
        url = (p.get("url") if isinstance(p, dict) else str(p)) or ""
        url = url.strip()
        key = url.rstrip("/")
        if url.startswith("http") and key not in seen:
            seen.add(key)
            others.append(url)
    return others


def select_pages():
    others = load_other_pages()
    slots = max(MAX_PAGES_PER_RUN - 1, 0)
    if not others or slots == 0:
        return [SITE_URL], others
    slot_index = int(time.time() // 43200)
    start = (slot_index * slots) % len(others)
    picked = [others[(start + i) % len(others)] for i in range(min(slots, len(others)))]
    untested = [u for u in others if u not in picked]
    return [SITE_URL] + picked, untested


def merge_status(statuses):
    if "DOWN" in statuses:
        return "DOWN"
    if "ERROR" in statuses:
        return "ERROR"
    if statuses and all(s == "UP" for s in statuses):
        return "UP"
    return "UNKNOWN"


async def main():
    t0 = time.monotonic()
    deadline = t0 + TOTAL_BUDGET_SECONDS

    targets, untested = select_pages()
    print("Pages à tester : " + ", ".join(targets))

    page_entries = []
    tested_statuses = []
    models_used = []
    completed_any = False
    stopped = False

    for i, url in enumerate(targets):
        if stopped:
            page_entries.append({
                "url": url, "status": "UNKNOWN", "http_code": None,
                "action_tested": None, "assertion_passed": False,
                "note": "Non testée : quota ou erreur Cloudflare.",
            })
            continue

        requirements = REQUIREMENTS if i == 0 else PAGE_REQUIREMENTS
        report, partial, stop = await test_page(url, requirements, deadline)
        stopped = stopped or stop

        if report is not None:
            entry = collapse_to_page(report, url)
            models_used.append(report.get("model_used"))
            completed_any = True
        elif partial[0] is not None:
            entry = partial[0]
        else:
            entry = {
                "url": url, "status": "ERROR", "http_code": None,
                "action_tested": None, "assertion_passed": False,
                "note": "Échec complet des essais.",
            }
        page_entries.append(entry)
        tested_statuses.append(entry["status"])

    for url in untested:
        page_entries.append({
            "url": url, "status": "UNKNOWN", "http_code": None,
            "action_tested": None, "assertion_passed": False,
            "note": "Non testée ce run (rotation).",
        })

    final_report = {
        "overall_status": merge_status(tested_statuses),
        "site_type": SITE_TYPE,
        "actions_completed": completed_any,
        "model_used": models_used[-1] if models_used else None,
        "pages": page_entries,
    }
    if not completed_any:
        final_report["error"] = "Aucun rapport complet remis par l'agent."

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("Durée totale : %.0fs" % (time.monotonic() - t0))
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
