#!/usr/bin/env python3
"""
Teste chaque fournisseur LLM avant de l'ajouter a la chaine de run_monitor.py.

Trois tests par fournisseur (sequentiels), fournisseurs en parallele :
  T1  message simple (contenu = chaine)             -> la cle et le modele marchent-ils ?
  T2  conversation avec contenu EN LISTE            -> le format qu'envoie openbrowser-ai
      ([{"type":"text","text":...}] sur user ET assistant ; Groq l'a refuse :
       "messages[4].content must be a string")
  T3  long prompt (~8 000 tokens) + consigne de   -> taille reelle des prompts de l'agent
      repondre par un bloc ```python                (6 000 a 14 000 tokens par etape)

Verdict : VIABLE (3/3), PARTIEL (T1 seul), KO.

Usage :
  python test_providers.py                       # tous les fournisseurs dont la cle existe
  python test_providers.py --only mistral,nvidia
  python test_providers.py --list-models         # affiche les IDs de modeles disponibles

Surcharge d'un fournisseur : <NOM>_MODEL et <NOM>_BASE_URL (ex. MISTRAL_MODEL=...).
Kilo et AionLabs n'ont pas de valeurs par defaut (je ne connais pas leurs endpoints) :
il faut definir KILO_BASE_URL / KILO_MODEL et AIONLABS_BASE_URL / AIONLABS_MODEL.
Les valeurs par defaut ci-dessous sont a VERIFIER : les catalogues changent souvent
(utilise --list-models si un modele est "introuvable").

Les cles ne sont jamais affichees.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI

TIMEOUT = float(os.environ.get("TEST_TIMEOUT_SECONDS", "100"))

# name, variable de cle, base_url par defaut, modele par defaut
PROVIDERS = [
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openrouter/free"),
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("google", "GOOGLE_API_KEY",
     "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash"),
    ("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest"),
    ("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1",
     "meta/llama-3.3-70b-instruct"),
    ("cohere", "COHERE_API_KEY", "https://api.cohere.ai/compatibility/v1", "command-a-03-2025"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("hf", "HF_API_KEY", "https://router.huggingface.co/v1", "openai/gpt-oss-120b"),
    ("kilo", "KILO_API_KEY", None, None),
    ("aionlabs", "AIONLABS_API_KEY", None, None),
]


def build_long_prompt():
    """~8 000 tokens : regles + faux etat de page (DOM) + consigne de format."""
    rules = (
        "Tu es un agent QA. Tu controles un navigateur. Tu reponds UNIQUEMENT avec une "
        "phrase courte puis UN bloc ```python. Jamais de balises <tool_call>.\n\n"
    )
    dom_lines = []
    for i in range(1, 165):
        dom_lines.append(
            "[%d]<a href=/produits/categorie-%d>Categorie numero %d - Materiel informatique "
            "et accessoires</a> | [%d]<button>Ajouter au panier</button> | "
            "Prix : %d FCFA | Livraison gratuite des 50 000 FCFA" % (i, i, i, i + 1000, 10000 + i * 137)
        )
    dom = "ETAT DE LA PAGE (extrait) :\n" + "\n".join(dom_lines) + "\n\n"
    ask = (
        "TACHE : clique sur l'element d'index 3 puis affiche le titre de la page. "
        "Reponds avec un bloc ```python qui contient await click(index=3) puis "
        "print(await evaluate('document.title'))."
    )
    return rules + dom + ask


LONG_PROMPT = build_long_prompt()


def classify_error(exc):
    msg = ("%s: %s" % (type(exc).__name__, exc))[:300].replace("\n", " ")
    low = msg.lower()
    if "429" in low or "quota" in low or "rate limit" in low or "too many" in low:
        kind = "quota/limite"
    elif "401" in low or "403" in low or "invalid api key" in low or "unauthorized" in low:
        kind = "cle refusee"
    elif "404" in low or "not found" in low or "does not exist" in low:
        kind = "modele introuvable (voir --list-models)"
    elif isinstance(exc, asyncio.TimeoutError) or "timeout" in low or "timed out" in low:
        kind = "timeout"
    elif "must be a string" in low or "content" in low and "string" in low:
        kind = "refuse le contenu en liste"
    else:
        kind = "erreur"
    return kind, msg


async def chat(client, model, messages, max_tokens):
    t0 = time.monotonic()
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens, temperature=0
        ),
        timeout=TIMEOUT,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text, time.monotonic() - t0


async def run_test(label, coro_factory, check):
    """Retourne {"ok":bool, "seconds":float|None, "detail":str, "kind":str|None}."""
    try:
        text, secs = await coro_factory()
    except Exception as exc:  # noqa: BLE001 : on veut tout capturer et classer
        kind, msg = classify_error(exc)
        return {"ok": False, "seconds": None, "detail": msg, "kind": kind}
    if not text:
        return {"ok": False, "seconds": round(secs, 1),
                "detail": "reponse vide (raisonnement qui mange max_tokens ?)",
                "kind": "vide"}
    ok, detail = check(text)
    return {"ok": ok, "seconds": round(secs, 1), "detail": detail, "kind": None if ok else "format"}


async def test_provider(name, key, base_url, model):
    client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=TIMEOUT, max_retries=0)
    result = {"provider": name, "model": model, "tests": {}, "verdict": "KO"}

    # T1
    t1 = await run_test(
        "T1",
        lambda: chat(client, model,
                     [{"role": "user", "content": "Reponds uniquement par le mot OK."}], 600),
        lambda t: ("ok" in t.lower(), t[:60]),
    )
    result["tests"]["T1_simple"] = t1
    if not t1["ok"] and t1["kind"] in ("cle refusee", "modele introuvable (voir --list-models)"):
        result["verdict"] = "KO"
        return result

    # T2
    msgs = [
        {"role": "system", "content": "Tu es un agent QA."},
        {"role": "user", "content": [{"type": "text", "text": "Etape 1 : dis bonjour."}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Bonjour."}]},
        {"role": "user", "content": [{"type": "text",
                                       "text": "Etape 2 : reponds uniquement par le mot OK."}]},
    ]
    t2 = await run_test(
        "T2",
        lambda: chat(client, model, msgs, 600),
        lambda t: ("ok" in t.lower(), t[:60]),
    )
    result["tests"]["T2_contenu_liste"] = t2

    # T3
    def check_format(text):
        low = text.lower()
        if "<tool_call>" in low or "<arg_key>" in low:
            return False, "format <tool_call> (pas de bloc python)"
        if "```python" in low and "click" in low:
            return True, "bloc python OK"
        return False, "pas de bloc ```python exploitable : " + text[:80].replace("\n", " ")

    t3 = await run_test(
        "T3",
        lambda: chat(client, model, [{"role": "user", "content": LONG_PROMPT}], 800),
        check_format,
    )
    result["tests"]["T3_long_prompt_format"] = t3

    passed = sum(1 for t in (t1, t2, t3) if t["ok"])
    if passed == 3:
        result["verdict"] = "VIABLE"
    elif t1["ok"]:
        result["verdict"] = "PARTIEL"
    return result


async def list_models(name, key, base_url):
    client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=30, max_retries=0)
    try:
        page = await client.models.list()
        ids = sorted(m.id for m in page.data)
        print("\n[%s] %d modeles (60 premiers) :" % (name, len(ids)))
        for mid in ids[:60]:
            print("   " + mid)
    except Exception as exc:  # noqa: BLE001
        print("\n[%s] liste impossible : %s" % (name, classify_error(exc)[1]))


def resolve(only):
    entries = []
    for name, key_env, default_url, default_model in PROVIDERS:
        if only and name not in only:
            continue
        key = os.environ.get(key_env, "")
        if not key:
            print("- %-10s ignore : %s absente" % (name, key_env))
            continue
        base_url = os.environ.get(name.upper() + "_BASE_URL", default_url)
        model = os.environ.get(name.upper() + "_MODEL", default_model)
        if not base_url or not model:
            print("- %-10s ignore : definir %s_BASE_URL et %s_MODEL" % (
                name, name.upper(), name.upper()))
            continue
        entries.append((name, key, base_url, model))
    return entries


def print_table(results):
    print("\n" + "=" * 100)
    print("%-11s %-32s %-9s %-14s %-14s %-16s" % (
        "fournisseur", "modele", "verdict", "T1 simple", "T2 liste", "T3 long+format"))
    print("-" * 100)

    def cell(t):
        if t is None:
            return "-"
        if t["ok"]:
            return "OK %.1fs" % t["seconds"]
        return "KO (%s)" % (t["kind"] or "?")

    for r in results:
        tests = r["tests"]
        print("%-11s %-32s %-9s %-14s %-14s %-16s" % (
            r["provider"], r["model"][:32], r["verdict"],
            cell(tests.get("T1_simple"))[:14],
            cell(tests.get("T2_contenu_liste"))[:14],
            cell(tests.get("T3_long_prompt_format"))[:16]))
    print("=" * 100)
    for r in results:
        for tname, t in r["tests"].items():
            if not t["ok"]:
                print("  %s / %s : %s" % (r["provider"], tname, t["detail"]))


def write_summary(results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Test des fournisseurs LLM", "",
             "| Fournisseur | Modele | Verdict | T1 | T2 (liste) | T3 (long+format) |",
             "|---|---|---|---|---|---|"]

    def md(t):
        if t is None:
            return "-"
        return ("OK %.1fs" % t["seconds"]) if t["ok"] else "KO : %s" % (t["kind"] or "?")

    for r in results:
        t = r["tests"]
        lines.append("| %s | %s | **%s** | %s | %s | %s |" % (
            r["provider"], r["model"], r["verdict"], md(t.get("T1_simple")),
            md(t.get("T2_contenu_liste")), md(t.get("T3_long_prompt_format"))))
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="noms separes par des virgules")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()
    only = [x.strip().lower() for x in args.only.split(",") if x.strip()]

    entries = resolve(only)
    if not entries:
        print("Aucun fournisseur a tester.")
        sys.exit(1)

    if args.list_models:
        await asyncio.gather(*(list_models(n, k, u) for n, k, u, _ in entries))
        return

    print("\nPrompt long : %d caracteres (~%d tokens)" % (len(LONG_PROMPT), len(LONG_PROMPT) // 4))
    print("Test de %d fournisseur(s)..." % len(entries))
    results = await asyncio.gather(*(test_provider(*e) for e in entries))
    results = list(results)

    print_table(results)
    with open("providers_report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    write_summary(results)

    viable = [r["provider"] + "/" + r["model"] for r in results if r["verdict"] == "VIABLE"]
    print("\nViables pour la chaine : " + (", ".join(viable) if viable else "aucun"))


if __name__ == "__main__":
    asyncio.run(main())
