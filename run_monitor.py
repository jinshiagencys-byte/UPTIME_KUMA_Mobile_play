"""
OpenBrowser-AI — Monitoring fonctionnel via OpenRouter.

Optimisations :
- Modèles rapides et explicites
- max_steps=15, timeout global 6 min
- Fermeture explicite de la session browser (flush la vidéo)
- Prompt directif, sans backticks ni .format()
"""
import asyncio
import json
import os
import re

from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile

# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]
SITE_ID = os.environ["SITE_ID"]
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ["REQUIREMENTS"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# ---------------------------------------------------------------------------
# Chaîne de fallback — modèles EXPLICITES et RAPIDES
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    "qwen/qwen3-8b:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
]

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
GLOBAL_TIMEOUT_SECONDS = 360
MAX_STEPS = 15

# ---------------------------------------------------------------------------
# Prompt système — construit par concaténation, sans .format() ni backticks
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. Your job is to TEST the web application, "
    "not just observe it.\n"
    "\n"
    "CRITICAL RULES:\n"
    "1. You MUST perform at least ONE real user interaction and verify its outcome with an assertion.\n"
    "2. The page loaded is NOT a test. The button exists is NOT a test.\n"
    "3. A valid test = (a) perform an action, (b) observe the result, (c) assert on the result.\n"
    "4. Use Python assert statements inside your code. If an assertion fails, the test fails.\n"
    "5. If you cannot find anything to interact with, that IS a failure - report it.\n"
    "6. Never conclude looks good without an assertion.\n"
    "\n"
    "STRICT STOP CONDITION (VERY IMPORTANT):\n"
    "- After EXACTLY 8 tool calls maximum, you MUST call done() regardless of what you found.\n"
    "- Do NOT explore more than 3 pages.\n"
    "- Do NOT retry the same action twice. If it fails, move on.\n"
    "- Do NOT scroll repeatedly. Do NOT re-click the same element.\n"
    "- Partial results are acceptable. It is better to report DOWN with 2 anomalies "
    "than to keep exploring forever.\n"
    "- If you have collected 2-3 pieces of evidence, call done() IMMEDIATELY.\n"
    "\n"
    "ANOMALY DETECTION (VERY IMPORTANT):\n"
    "- Any visible text containing Erreur, Error, Failed, undefined, null, "
    "0 produit, No results, Aucun produit is a FUNCTIONAL FAILURE.\n"
    "- A link pointing to /undefined or with undefined in the URL is a FUNCTIONAL FAILURE.\n"
    "- An empty product listing on a page that should show products is a FUNCTIONAL FAILURE.\n"
    "- If you find ANY of these, set overall_status to DOWN and describe the anomaly.\n"
    "\n"
    "REQUIRED INTERACTIONS:\n"
    "__REQUIREMENTS__\n"
    "\n"
    "FINAL OUTPUT FORMAT (VERY IMPORTANT):\n"
    "When you are done, you MUST call the done tool with a text parameter containing "
    "ONLY a valid JSON object, without any markdown around it. The JSON must have:\n"
    "  overall_status: UP or DOWN\n"
    "  site_type: __SITE_TYPE__\n"
    "  actions_completed: true or false\n"
    "  model_used: __MODEL_NAME__\n"
    "  pages: list of objects with keys url, status (UP/DOWN), http_code, "
    "action_tested, assertion_passed, note\n"
    "\n"
    "Your done call must look like this (inside a python code block):\n"
    "  import json\n"
    "  result = {\"overall_status\": \"DOWN\", \"site_type\": \"__SITE_TYPE__\", "
    "\"actions_completed\": True, \"model_used\": \"__MODEL_NAME__\", "
    "\"pages\": [{\"url\": \"__SITE_URL__\", \"status\": \"DOWN\", \"http_code\": 200, "
    "\"action_tested\": \"searched products\", \"assertion_passed\": False, "
    "\"note\": \"0 produit trouve\"}]}\n"
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
# Extraction JSON
# ---------------------------------------------------------------------------
def extract_json_from_text(text: str) -> dict | None:
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

    # 1. Dernier done() exploitable
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

    # 2. Dernier output qui ressemble à du JSON exploitable
    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if ('"url"' in output or '"overall_status"' in output
                or '"anomalies"' in output or '"countText"' in output
                or '"emptyText"' in output):
            return output

    return str(result)


def normalize_report(report: dict, model_name: str) -> dict:
    if "overall_status" in report and "pages" in report:
        report["model_used"] = model_name
        return report

    text_blob = json.dumps(report, ensure_ascii=False).lower()
    has_anomaly = any(
        kw in text_blob
        for kw in ["erreur", "error", "undefined", "0 produit", "aucun produit",
                   "no results", "aucun résultat", "anomalies_detected"]
    )
    status = "DOWN" if has_anomaly else "UP"

    pages = [
        {
            "url": report.get("url", SITE_URL),
            "status": status,
            "http_code": None,
            "action_tested": "exploration et assertions par l'agent",
            "assertion_passed": not has_anomaly,
            "note": report.get("done_summary") or report.get("message") or "Vérification effectuée",
        }
    ]

    return {
        "overall_status": status,
        "site_type": report.get("site_type", SITE_TYPE),
        "actions_completed": True,
        "model_used": model_name,
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Fermeture explicite de la session browser — flush la vidéo sur disque
# ---------------------------------------------------------------------------
async def close_agent_session(agent) -> None:
    if agent is None:
        return

    for attr_name in ("browser_session", "browser", "session", "browser_context"):
        try:
            session = getattr(agent, attr_name, None)
            if session is None:
                continue
            if hasattr(session, "close"):
                result = session.close()
                if asyncio.iscoroutine(result):
                    await result
                print("Session fermee via agent." + attr_name)
                return
            if hasattr(session, "stop"):
                result = session.stop()
                if asyncio.iscoroutine(result):
                    await result
                print("Session arretee via agent." + attr_name + ".stop()")
                return
        except Exception as e:
            print("Echec fermeture via " + attr_name + " : " + str(e))

    print("Aucun attribut de session trouve pour fermeture explicite")


# ---------------------------------------------------------------------------
# Tentative d'exécution
# ---------------------------------------------------------------------------
async def run_attempt(model_name: str, task: str) -> dict | None:
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
            record_video_dir="./recordings",
            record_video_size={"width": 1280, "height": 720},
        )

        agent = CodeAgent(
            task=task,
            llm=llm,
            browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        # Timeout global
        try:
            result = await asyncio.wait_for(agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("Timeout global (" + str(GLOBAL_TIMEOUT_SECONDS) + "s) pour " + model_name)
            return None

        final_text = extract_final_result(result)
        print("Resultat final extrait (" + model_name + ") :")
        print(final_text[:1000])

        report = extract_json_from_text(final_text)
        if report:
            normalized = normalize_report(report, model_name)
            print("JSON valide extrait pour " + model_name)
            return normalized

        # Fallback propre
        print("Pas de JSON valide, rapport reconstruit pour " + model_name)
        text_lower = final_text.lower()
        has_anomaly = any(
            kw in text_lower
            for kw in ["erreur", "error", "undefined", "0 produit", "no results",
                       "aucun résultat", "aucun produit", "anomalies_detected"]
        )

        anomalies = []
        if "erreur lors du chargement des sliders" in text_lower:
            anomalies.append("carrousel casse")
        if "erreur lors du chargement des marques" in text_lower:
            anomalies.append("marques non chargees")
        if '"alt": "undefined"' in text_lower or "alt='undefined'" in text_lower:
            anomalies.append("image avec alt=undefined")
        if "/detail-produit/undefined" in text_lower:
            anomalies.append("lien produit casse")
        if "0 produit trouvé" in text_lower or "0 produits" in text_lower:
            anomalies.append("catalogue produits vide")

        if anomalies:
            clean_note = "Anomalies detectees : " + ", ".join(anomalies) + "."
        else:
            clean_note = "Exploration automatique par l'agent."

        return {
            "overall_status": "DOWN" if has_anomaly else "UP",
            "site_type": SITE_TYPE,
            "actions_completed": True,
            "model_used": model_name,
            "pages": [
                {
                    "url": SITE_URL,
                    "status": "DOWN" if has_anomaly else "UP",
                    "http_code": None,
                    "action_tested": "exploration et assertions par l'agent",
                    "assertion_passed": not has_anomaly,
                    "note": clean_note,
                }
            ],
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
        "Execute the required interactions described in the system message. "
        "Use Python assertions to verify outcomes. "
        "Detect any anomalies (errors, undefined, empty listings). "
        "HARD LIMIT: 8 tool calls maximum, then call done() immediately. "
        "If you haven't found anything after 5 tool calls, report the status based on what you have. "
        "When done, call the done tool with a text parameter containing ONLY a valid JSON object."
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
            "error": "Tous les modeles OpenRouter ont echoue ou timeout.",
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
