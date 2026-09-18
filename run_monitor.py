"""
OpenBrowser-AI — Monitoring fonctionnel via OpenRouter.

Détection dynamique des modèles gratuits fiables + screenshots + timelapse.
"""
import asyncio
import json
import os
import re
import urllib.request

from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile

# ---------------------------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]
SITE_ID = os.environ["SITE_ID"]
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ["REQUIREMENTS"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

RECORDINGS_DIR = os.path.abspath("./recordings")
SHOTS_DIR = os.path.join(RECORDINGS_DIR, "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)
print("Recordings dir : " + RECORDINGS_DIR)
print("Shots dir : " + SHOTS_DIR)

# ---------------------------------------------------------------------------
# Détection des modèles gratuits fiables
# ---------------------------------------------------------------------------
TRUSTED_PROVIDERS = (
    "google/", "meta-llama/", "mistralai/", "qwen/", "deepseek/",
    "microsoft/", "nousresearch/", "cohere/", "amazon/", "ai21/",
)


def get_working_free_models() -> list:
    """Récupère les modèles gratuits avec tool calling, chez des providers fiables."""
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        candidates = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid.endswith(":free"):
                continue
            if not any(mid.startswith(p) for p in TRUSTED_PROVIDERS):
                continue
            pricing = m.get("pricing", {})
            if str(pricing.get("prompt")) not in ("0", "0.0", "0.0000"):
                continue
            if str(pricing.get("completion")) not in ("0", "0.0", "0.0000"):
                continue
            supported = m.get("supported_parameters", [])
            if "tools" not in supported:
                continue
            candidates.append(mid)

        def score(mid):
            if "gemini" in mid: return 0
            if "llama-3.3" in mid: return 1
            if "llama-3.1" in mid: return 2
            if "mistral" in mid: return 3
            if "qwen-2.5-72b" in mid: return 4
            if "qwen-2.5" in mid: return 5
            if "deepseek-chat" in mid: return 6
            if "deepseek" in mid: return 7
            if "hermes" in mid: return 8
            return 20

        candidates.sort(key=score)
        print("Modeles gratuits fiables : " + ", ".join(candidates[:8]))
        return candidates[:5]

    except Exception as e:
        print("Erreur listing modeles : " + str(e))
        return []


MODEL_CHAIN = get_working_free_models()

# Fallback si la détection échoue
if not MODEL_CHAIN:
    MODEL_CHAIN = [
        "google/gemini-2.0-flash-exp:free",
        "google/gemini-flash-1.5-8b:free",
        "mistralai/mistral-small-24b-instruct-2501:free",
        "qwen/qwen-2.5-72b-instruct:free",
    ]
    print("Fallback chain : " + str(MODEL_CHAIN))

print("Chaine finale : " + str(MODEL_CHAIN))

GLOBAL_TIMEOUT_SECONDS = 300
MAX_STEPS = 12

# ---------------------------------------------------------------------------
# Prompt système
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. TEST the web app, do not just observe.\n"
    "\n"
    "OUTPUT FORMAT (CRITICAL):\n"
    "You MUST write Python code blocks between triple backticks. Example:\n"
    "\n"
    "I will check the page title.\n"
    "```python\n"
    "title = await evaluate('document.title')\n"
    "print(title)\n"
    "```\n"
    "\n"
    "Do NOT use XML tags. Do NOT use tool_name(args) syntax.\n"
    "ONLY Python code blocks.\n"
    "\n"
    "RULES:\n"
    "1. Perform at least ONE real user interaction and verify with an assertion.\n"
    "2. Page loaded is NOT a test. Button exists is NOT a test.\n"
    "3. Valid test = action + observation + assertion.\n"
    "4. Use Python assert.\n"
    "\n"
    "STRICT STOP CONDITION:\n"
    "- After 6 tool calls max, call done() no matter what.\n"
    "- Partial results are OK. Better DOWN with 2 anomalies than infinite loops.\n"
    "\n"
    "ANOMALIES (functional failures):\n"
    "- Text containing Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.\n"
    "- Link with /undefined in URL.\n"
    "- Empty product listing on a catalog page.\n"
    "\n"
    "REQUIRED INTERACTIONS:\n"
    "__REQUIREMENTS__\n"
    "\n"
    "FINAL OUTPUT:\n"
    "Call done(text=...) with ONLY a valid JSON in text:\n"
    "  overall_status, site_type, actions_completed, model_used, pages[].\n"
    "\n"
    "Example done:\n"
    "```python\n"
    "import json\n"
    "result = {\"overall_status\": \"DOWN\", \"site_type\": \"__SITE_TYPE__\", "
    "\"actions_completed\": True, \"model_used\": \"__MODEL_NAME__\", "
    "\"pages\": [{\"url\": \"__SITE_URL__\", \"status\": \"DOWN\", "
    "\"http_code\": 200, \"action_tested\": \"search\", "
    "\"assertion_passed\": False, \"note\": \"0 produit\"}]}\n"
    "await done(text=json.dumps(result, ensure_ascii=False), success=True)\n"
    "```\n"
)


def build_system_prompt(model_name: str) -> str:
    return (
        SYSTEM_PROMPT_TEMPLATE
        .replace("__REQUIREMENTS__", REQUIREMENTS)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
        .replace("__SITE_URL__", SITE_URL)
    )


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def extract_json_from_text(text: str):
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
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


def extract_final_result(result) -> str:
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
        match = re.search(
            r"done\s*\(\s*text\s*=\s*json\.dumps\(\s*([a-zA-Z_][a-zA-Z0-9_]*)",
            source, re.DOTALL,
        )
        if match:
            output = getattr(cell, "output", "") or ""
            if output.strip():
                return output
            return source
    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if '"url"' in output or '"overall_status"' in output:
            return output
    return str(result)


def normalize_report(report: dict, model_name: str) -> dict:
    if "overall_status" in report and "pages" in report:
        report["model_used"] = model_name
        return report
    text_blob = json.dumps(report, ensure_ascii=False).lower()
    has_anomaly = any(
        kw in text_blob
        for kw in ["erreur", "error", "undefined", "0 produit",
                   "aucun produit", "no results", "anomalies_detected"]
    )
    status = "DOWN" if has_anomaly else "UP"
    pages = [{
        "url": report.get("url", SITE_URL),
        "status": status,
        "http_code": None,
        "action_tested": "exploration",
        "assertion_passed": not has_anomaly,
        "note": report.get("done_summary") or report.get("message") or "Verification effectuee",
    }]
    return {
        "overall_status": status,
        "site_type": report.get("site_type", SITE_TYPE),
        "actions_completed": True,
        "model_used": model_name,
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Récupération de la page Playwright depuis la session OpenBrowser-AI
# ---------------------------------------------------------------------------
async def get_playwright_page(session):
    """Essaie plusieurs méthodes pour récupérer la page Playwright."""
    if session is None:
        return None

    # 1. Méthode officielle
    for method_name in ("must_get_current_page", "get_current_page"):
        if hasattr(session, method_name):
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    r = await r
                if r is not None and hasattr(r, "screenshot"):
                    return r
            except Exception:
                pass

    # 2. Attributs directs
    for attr in ("current_page", "page", "_page"):
        try:
            p = getattr(session, attr, None)
            if p is not None and hasattr(p, "screenshot"):
                return p
        except Exception:
            pass

    # 3. Via get_pages()
    if hasattr(session, "get_pages"):
        try:
            r = session.get_pages()
            if asyncio.iscoroutine(r):
                r = await r
            if r and len(r) > 0:
                return r[0]
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Screenshot recorder
# ---------------------------------------------------------------------------
async def screenshot_recorder(agent, interval=2.0):
    """Prend un screenshot toutes les N secondes."""
    idx = 0
    misses = 0
    while True:
        try:
            await asyncio.sleep(interval)
            session = getattr(agent, "browser_session", None)
            if session is None:
                misses += 1
                continue

            page = await get_playwright_page(session)
            if page is None:
                misses += 1
                if misses > 30:
                    print("Screenshot recorder : abandon apres %d echecs" % misses)
                    break
                continue

            path = os.path.join(SHOTS_DIR, "shot_%04d.png" % idx)
            try:
                r = page.screenshot(path=path, full_page=False)
                if asyncio.iscoroutine(r):
                    await r
                idx += 1
                misses = 0
            except Exception:
                misses += 1

        except asyncio.CancelledError:
            print("Screenshot recorder arrete (%d shots)" % idx)
            break
        except Exception:
            misses += 1


# ---------------------------------------------------------------------------
# Fermeture de session (sans debug verbeux cette fois)
# ---------------------------------------------------------------------------
async def close_agent_session(agent) -> None:
    if agent is None:
        return

    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
            print("Session trouvee via agent." + attr_name)
            break

    if session is None:
        print("Aucune session trouvee")
        return

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
            print("Echec session." + method_name + "() : " + str(e))

    await asyncio.sleep(3)

    try:
        shots = os.listdir(SHOTS_DIR) if os.path.isdir(SHOTS_DIR) else []
        print("Screenshots captures : " + str(len(shots)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tentative
# ---------------------------------------------------------------------------
async def run_attempt(model_name: str, task: str):
    print("=" * 60)
    print("TENTATIVE - MODELE : " + model_name)
    print("=" * 60)

    agent = None
    recorder_task = None
    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            temperature=0.0,
        )

        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
        )

        agent = CodeAgent(
            task=task,
            llm=llm,
            browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        recorder_task = asyncio.create_task(screenshot_recorder(agent, interval=2.0))

        try:
            result = await asyncio.wait_for(agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        final_text = extract_final_result(result)
        print("Resultat extrait (" + model_name + ") :")
        print(final_text[:800])

        report = extract_json_from_text(final_text)
        if report:
            return normalize_report(report, model_name)

        # Fallback
        text_lower = final_text.lower()
        has_anomaly = any(
            kw in text_lower
            for kw in ["erreur", "error", "undefined", "0 produit",
                       "no results", "aucun produit", "anomalies_detected"]
        )
        anomalies = []
        if "erreur lors du chargement des sliders" in text_lower:
            anomalies.append("carrousel casse")
        if "erreur lors du chargement des marques" in text_lower:
            anomalies.append("marques non chargees")
        if "0 produit" in text_lower:
            anomalies.append("catalogue vide")

        clean_note = ("Anomalies detectees : " + ", ".join(anomalies) + ".") if anomalies \
            else "Exploration automatique (aucune anomalie detectee)."

        return {
            "overall_status": "DOWN" if has_anomaly else "UP",
            "site_type": SITE_TYPE,
            "actions_completed": True,
            "model_used": model_name,
            "pages": [{
                "url": SITE_URL,
                "status": "DOWN" if has_anomaly else "UP",
                "http_code": None,
                "action_tested": "exploration et assertions par l'agent",
                "assertion_passed": not has_anomaly,
                "note": clean_note,
            }],
        }

    except Exception as err:
        print("Echec avec " + model_name + " : " + str(err))
        return None
    finally:
        if recorder_task is not None:
            recorder_task.cancel()
            try:
                await recorder_task
            except (asyncio.CancelledError, Exception):
                pass
        await close_agent_session(agent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    task = (
        "Navigate to " + SITE_URL + ". "
        "Execute the required interactions. "
        "Use Python assertions. "
        "Detect anomalies (errors, undefined, empty listings). "
        "HARD LIMIT: 6 tool calls max, then call done(). "
        "IMPORTANT: write ONLY valid Python code blocks between triple backticks."
    )

    final_report = None
    for model_name in MODEL_CHAIN:
        report = await run_attempt(model_name, task)
        if report is not None:
            final_report = report
            break

    if final_report is None:
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [],
            "error": "Tous les modeles ont echoue.",
        }

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("")
    print("output.json ecrit dans " + output_path)
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
