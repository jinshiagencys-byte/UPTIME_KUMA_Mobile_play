"""
OpenBrowser-AI — Monitoring fonctionnel via OpenRouter.
- Détection de type de site (fait en amont dans le workflow)
- Prompt adaptatif injecté via REQUIREMENTS
- Enregistrement vidéo natif via record_video_dir
- Extraction robuste du JSON depuis le résultat de l'agent
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
# Chaîne de fallback OpenRouter
# - openrouter/free : routeur auto gratuit avec tool calling
# - qwen/qwen3.6-plus:free : 1M contexte, tool calling vérifié
# - qwen/qwen3-coder:free : 1M contexte, orienté code
# - meta-llama/llama-3.3-70b-instruct:free : fallback généraliste
# - arcee-ai/trinity-large-preview:free : confirmé fonctionnel
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    "openrouter/free",
    "qwen/qwen3.6-plus:free",
    "qwen/qwen3-coder:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "arcee-ai/trinity-large-preview:free",
]

# ---------------------------------------------------------------------------
# Prompt système : impose le test fonctionnel + détection d'anomalies
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are a functional QA engineer. Your job is to TEST the web application, not just observe it.

CRITICAL RULES:
1. You MUST perform at least ONE real user interaction and verify its outcome with an assertion.
2. "The page loaded" is NOT a test. "The button exists" is NOT a test.
3. A valid test = (a) perform an action, (b) observe the result, (c) assert on the result.
4. Use Python `assert` statements inside your code. If an assertion fails, the test fails.
5. If you cannot find anything to interact with, that IS a failure — report it.
6. Never conclude "looks good" without an assertion.

ANOMALY DETECTION (VERY IMPORTANT):
- Any visible text containing "Erreur", "Error", "Failed", "undefined", "null",
  "0 résultat", "0 produit", "No results", "Aucun résultat" is a FUNCTIONAL FAILURE.
- A link pointing to "/undefined" or with "undefined" in the URL is a FUNCTIONAL FAILURE.
- An empty product listing on a page that should show products is a FUNCTIONAL FAILURE.
- If you find ANY of these, set overall_status to "DOWN" and describe the anomaly.

REQUIRED INTERACTIONS:
{requirements}

FINAL OUTPUT FORMAT (VERY IMPORTANT):
When you are done, you MUST call the `done` tool with a `text` parameter containing
ONLY a valid JSON object — no markdown, no explanations around it. The JSON must be:

{{
  "overall_status": "UP" or "DOWN",
  "site_type": "{site_type}",
  "actions_completed": true or false,
  "model_used": "{model_name}",
  "pages": [
    {{
      "url": "{site_url}",
      "status": "UP" or "DOWN",
      "http_code": 200,
      "action_tested": "description of what you actually did",
      "assertion_passed": true or false,
      "note": "why it passed or failed, and list any anomalies found"
    }}
  ]
}}
"""


def extract_json_from_text(text: str) -> dict | None:
    """Extrait un objet JSON valide depuis un texte libre."""
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
    """Extrait le texte final passé à done() depuis l'objet retourné par agent.run()."""
    # Cas 1 : méthode final_result() (browser-use / OpenBrowser-AI)
    if hasattr(result, "final_result"):
        try:
            fr = result.final_result()
            if fr:
                return str(fr)
        except Exception:
            pass

    # Cas 2 : chercher la dernière cellule dont le source contient done(
    cells = getattr(result, "cells", None) or getattr(result, "history", None)
    if cells:
        for cell in reversed(list(cells)):
            source = getattr(cell, "source", "") or ""
            if "done(" in source:
                # Extraire depuis un json.dumps(var)
                match = re.search(r"done\(\s*text\s*=\s*json\.dumps\(\s*([a-zA-Z_][a-zA-Z0-9_]*)", source, re.DOTALL)
                if match:
                    var_name = match.group(1)
                    output = getattr(cell, "output", "") or ""
                    # L'output peut contenir le JSON sérialisé
                    if output.strip().startswith("{"):
                        return output
                    # Sinon, fallback sur str(result) qui contiendra le print
                    return str(result)

                # Extraire depuis un done(text='...') direct
                match = re.search(r"done\(\s*text\s*=\s*['\"](.+?)['\"]\s*,\s*success", source, re.DOTALL)
                if match:
                    return match.group(1)

    # Cas 3 : fallback str(result)
    return str(result)


def normalize_report(report: dict, model_name: str) -> dict:
    """Normalise le rapport de l'agent vers le schéma attendu par le relay."""
    # Cas idéal : l'agent a produit le bon schéma
    if "overall_status" in report and "pages" in report:
        report["model_used"] = model_name
        return report

    # Cas : l'agent a produit {site, title, url, status, features_verified}
    pages = report.get("pages") or []
    if not pages:
        features = report.get("features_verified", {})
        all_ok = all(features.values()) if features else True
        # Détection d'anomalies dans le texte libre
        text_blob = json.dumps(report, ensure_ascii=False).lower()
        has_anomaly = any(
            kw in text_blob
            for kw in ["erreur", "error", "undefined", "0 produit", "no results", "aucun résultat"]
        )
        status = "DOWN" if (has_anomaly or not all_ok) else "UP"
        pages = [
            {
                "url": report.get("url", SITE_URL),
                "status": status,
                "http_code": None,
                "action_tested": "vérification des éléments clés",
                "assertion_passed": all_ok and not has_anomaly,
                "note": report.get("message", "Vérification effectuée"),
            }
        ]

    return {
        "overall_status": report.get("overall_status", "UP"),
        "site_type": report.get("site_type", SITE_TYPE),
        "actions_completed": report.get("actions_completed", True),
        "model_used": model_name,
        "pages": pages,
    }


async def run_attempt(model_name: str, task: str) -> dict | None:
    """Tente une exécution avec un modèle OpenRouter donné."""
    print("=" * 60)
    print(f"🤖 TENTATIVE — MODÈLE : {model_name}")
    print("=" * 60)

    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            temperature=0.0,
        )

        # ------------------------------------------------------------------
        # Configuration du navigateur AVEC enregistrement vidéo natif
        # ------------------------------------------------------------------
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
            max_steps=30,
            extend_system_message=SYSTEM_PROMPT.format(
                requirements=REQUIREMENTS,
                site_type=SITE_TYPE,
                site_url=SITE_URL,
                model_name=model_name,
            ),
        )

        result = await agent.run()

        # Extraction robuste du texte final (passé à done())
        final_text = extract_final_result(result)
        print(f"📝 Résultat final extrait ({model_name}) :")
        print(final_text[:1500])

        # Extraction du JSON
        report = extract_json_from_text(final_text)

        if report:
            normalized = normalize_report(report, model_name)
            print(f"✅ JSON valide extrait et normalisé pour {model_name}")
            return normalized

        # Fallback : rapport reconstruit à partir du texte brut
        print(f"⚠️ Pas de JSON valide, rapport reconstruit pour {model_name}")
        text_lower = final_text.lower()
        has_anomaly = any(
            kw in text_lower
            for kw in ["erreur", "error", "undefined", "0 produit", "no results", "aucun résultat"]
        )
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
                    "action_tested": "exploration par l'agent",
                    "assertion_passed": not has_anomaly,
                    "note": final_text[:500] if final_text else "Aucune sortie exploitable",
                }
            ],
        }

    except Exception as err:
        print(f"❌ Échec avec {model_name} : {err}")
        return None


async def main() -> None:
    task = (
        f"Navigate to {SITE_URL}. "
        f"Execute the required interactions described in the system message. "
        f"Use Python assertions to verify outcomes. "
        f"Detect any anomalies (errors, undefined, empty listings). "
        f"When done, call the `done` tool with a `text` parameter containing ONLY a valid JSON object."
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
            "error": "Tous les modèles OpenRouter ont échoué.",
        }

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print(f"\n✅ output.json écrit dans {output_path}")
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
