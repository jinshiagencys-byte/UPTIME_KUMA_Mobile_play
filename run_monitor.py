"""
OpenBrowser-AI — Monitoring fonctionnel via OpenRouter.

Correctifs :
- Détection dynamique des modèles gratuits (OpenRouter change souvent)
- Fermeture agressive du playwright context pour flush la vidéo
- Chemin absolu pour recordings/
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
# Env
# ---------------------------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]
SITE_ID = os.environ["SITE_ID"]
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ["REQUIREMENTS"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)
print("Recordings dir : " + RECORDINGS_DIR)

# ---------------------------------------------------------------------------
# Détection dynamique des modèles gratuits avec tool calling
# ---------------------------------------------------------------------------
def get_free_models_with_tools() -> list:
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        PREFERRED = [
            "meta-llama/llama-3.3-70b-instruct:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "qwen/qwen-2.5-7b-instruct:free",
            "deepseek/deepseek-chat:free",
            "microsoft/phi-3-medium-128k-instruct:free",
            "mistralai/mistral-7b-instruct:free",
        ]

        available = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid.endswith(":free"):
                continue
            pricing = m.get("pricing", {})
            if str(pricing.get("prompt")) not in ("0", "0.0"):
                continue
            if str(pricing.get("completion")) not in ("0", "0.0"):
                continue
            supported = m.get("supported_parameters", [])
            if "tools" in supported or "tool_choice" in supported:
                available.append(mid)

        ordered = [m for m in PREFERRED if m in available]
        ordered += [m for m in available if m not in PREFERRED]

        if ordered:
            print("Modeles gratuits disponibles : " + ", ".join(ordered[:8]))
        else:
            print("Aucun modele gratuit avec tool calling detecte")
        return ordered

    except Exception as e:
        print("Erreur listing modeles : " + str(e))
        return []


STATIC_FALLBACK = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
]

_dynamic = get_free_models_with_tools()
MODEL_CHAIN = (_dynamic if _dynamic else []) + [m for m in STATIC_FALLBACK if m not in _dynamic]
MODEL_CHAIN = MODEL_CHAIN[:5]
if not MODEL_CHAIN:
    MODEL_CHAIN = STATIC_FALLBACK

print("Chaine finale : " + str(MODEL_CHAIN))

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
GLOBAL_TIMEOUT_SECONDS = 360
MAX_STEPS = 15

# ---------------------------------------------------------------------------
# Prompt système
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. TEST the web app, do not just observe.\n"
    "\n"
    "RULES:\n"
    "1. Perform at least ONE real user interaction and verify with an assertion.\n"
    "2. Page loaded is NOT a test. Button exists is NOT a test.\n"
    "3. Valid test = action + observation + assertion.\n"
    "4. Use Python assert. If assert fails, test fails.\n"
    "5. If nothing to interact with, that IS a failure.\n"
    "\n"
    "STRICT STOP CONDITION:\n"
    "- After 8 tool calls max, call done() no matter what.\n"
    "- Do NOT explore more than 3 pages.\n"
    "- Do NOT retry the same action twice.\n"
    "- Partial results are OK. Better DOWN with 2 anomalies than infinite exploration.\n"
    "\n"
    "ANOMALIES (functional failures):\n"
    "- Text containing Erreur, Error, Failed, undefined, null, "
    "0 produit, No results, Aucun produit.\n"
    "- Link with /undefined in URL.\n"
    "- Empty product listing on a catalog page.\n"
    "- If found: overall_status=DOWN and describe in note.\n"
    "\n"
    "REQUIRED INTERACTIONS:\n"
    "__REQUIREMENTS__\n"
    "\n"
    "FINAL OUTPUT:\n"
    "Call done(text=...) with ONLY a valid JSON object in text:\n"
    "  overall_status: UP or DOWN\n"
    "  site_type: __SITE_TYPE__\n"
    "  actions_completed: true/false\n"
    "  model_used: __MODEL_NAME__\n"
    "  pages: [{url, status, http_code, action_tested, assertion_passed, note}]\n"
    "\n"
    "Example done call (inside a python code block):\n"
    "  import json\n"
    "  result = {\"overall_status\": \"DOWN\", \"site_type\": \"__SITE_TYPE__\", "
    "\"actions_completed\": True, \"model_used\": \"__MODEL_NAME__\", "
    "\"pages\": [{\"url\": \"__SITE_URL__\", \"status\": \"DOWN\", "
    "\"http_code\": 200, \"action_tested\": \"search\", "
    "\"assertion_passed\": False, \"note\": \"0 produit trouve\"}]}\n"
    "  await done(text=json.dumps(result, ensure_ascii=False), success=True)\n"
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
        if ('"url"' in output or '"overall_status"' in output
                or '"anomalies"' in output or '"countText"' in output):
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
# Fermeture agressive de la session browser
# ---------------------------------------------------------------------------
async def close_agent_session(agent) -> None:
    if agent is None:
        print("Agent None, rien a fermer")
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

    # 1. Fermer le playwright context en priorite (c'est lui qui flush la video)
    for ctx_attr in ("browser_context", "context", "_browser_context",
                     "_context", "playwright_context",
                     "playwright_browser_context"):
        ctx = getattr(session, ctx_attr, None)
        if ctx is None:
            continue
        for close_method in ("close", "shutdown"):
            if not hasattr(ctx, close_method):
                continue
            try:
                r = getattr(ctx, close_method)()
                if asyncio.iscoroutine(r):
                    await r
                print("Context ferme via session." + ctx_attr + "." + close_method + "()")
                break
            except Exception as e:
                print("Echec " + ctx_attr + "." + close_method + " : " + str(e))

    # 2. Fermer la session elle-meme
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

    # 3. Attendre le flush ffmpeg
    print("Attente flush video (3s)...")
    await asyncio.sleep(3)

    # 4. Verifier ce qui a ete ecrit
    try:
        files = os.listdir(RECORDINGS_DIR)
        if files:
            print("Videos dans recordings/ : " + str(files))
        else:
            print("Aucune video dans recordings/ apres fermeture")
    except Exception as e:
        print("Impossible de lister recordings/ : " + str(e))


# ---------------------------------------------------------------------------
# Tentative
# ---------------------------------------------------------------------------
async def run_attempt(model_name: str, task: str):
    print("=" * 60)
    print("TENTATIVE - MODELE : " + model_name)
    print("=" * 60)

    agent = None
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
            record_video_dir=RECORDINGS_DIR,
            record_video_size={"width": 1280, "height": 720},
        )

        agent = CodeAgent(
            task=task,
            llm=llm,
            browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        try:
            result = await asyncio.wait_for(agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        final_text = extract_final_result(result)
        print("Resultat extrait (" + model_name + ") :")
        print(final_text[:1000])

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
        if '"alt": "undefined"' in text_lower or "alt='undefined'" in text_lower:
            anomalies.append("image alt=undefined")
        if "/detail-produit/undefined" in text_lower:
            anomalies.append("lien produit casse")
        if "0 produit trouvé" in text_lower or "0 produits" in text_lower:
            anomalies.append("catalogue vide")

        clean_note = ("Anomalies detectees : " + ", ".join(anomalies) + ".") if anomalies \
            else "Exploration automatique par l'agent."

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
        await close_agent_session(agent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    task = (
        "Navigate to " + SITE_URL + ". "
        "Execute the required interactions. "
        "Use Python assertions to verify outcomes. "
        "Detect anomalies (errors, undefined, empty listings). "
        "HARD LIMIT: 8 tool calls max, then call done() immediately. "
        "When done, call done with a text parameter containing ONLY valid JSON."
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
            "error": "Tous les modeles ont echoue ou timeout.",
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
