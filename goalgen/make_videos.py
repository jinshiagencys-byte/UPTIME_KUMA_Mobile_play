#!/usr/bin/env python3
"""Assemble les frames JPEG de run_goals.py --record-frames en une MP4 par goal.

Pourquoi pas un simple -pattern_type glob : jev nomme chaque frame
<elapsed_ms:06d>.jpg (jev_ultrafast/agent.py), donc l'ecart entre deux noms
CONTIENT la duree reelle de la frame precedente. Un glob a fps fixe ecraserait
cette information : un agent bloque 10 s devant une page donnerait un simple
clignotement. Ici on ecrit un fichier de concat ffmpeg avec une duration par
frame, donc la video rejoue le run au temps reel.

Usage:
    python3 goalgen/make_videos.py --runs-dir runs

Sort toujours 0 : la video est un bonus de debug, elle ne doit pas faire
echouer un run de monitoring deja concluant. Les echecs sont imprimes dans le log.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Cadences bornees : en dessous le viewer lit un glitch, au-dessus une etape
# bloquee gele l'image pendant plusieurs secondes.
MIN_FRAME_S = 0.35
MAX_FRAME_S = 6.0
# La derniere frame represente l'etat final du run : on la laisse poser 1 s avant
# de rendre la main au viewer, sinon elle passe si vite qu'elle est invisible.
END_HOLD_S = 1.0


def numbered(frames_dir):
    """(elapsed_ms, chemin) tries par temps reel, en ignorant les noms non numeriques."""
    entries = []
    for frame in frames_dir.glob("*.jpg"):
        try:
            entries.append((int(frame.stem), frame))
        except ValueError:
            print(f"[video] frame ignore (nom non numerique) : {frame.name}")
    entries.sort(key=lambda entry: entry[0])
    return entries


def write_concat(entries, list_path):
    """Format concat demuxer : chaque `file` est suivi de sa `duration`, qui
    s'applique AU `file` qui la precede.

    Toutes les frames, y compris la derniere, declarent une duree : une `file`
    sans `duration` herite de celle de la precedente, ce qui ferait compter la
    derniere image deux fois. D'ou l'absence de ligne `file` de repetition en fin
    de liste — le doublon classique du concat est inutile ici.
    """
    lines = []
    for i, (_, frame) in enumerate(entries):
        lines.append(f"file '{frame.resolve()}'")
        if i + 1 < len(entries):
            delta = (entries[i + 1][0] - entries[i][0]) / 1000.0
            duration = max(MIN_FRAME_S, min(delta, MAX_FRAME_S))
        else:
            duration = END_HOLD_S
        lines.append(f"duration {duration:.3f}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def encode(entries, out_path, scale):
    list_path = out_path.with_suffix(".txt")
    write_concat(entries, list_path)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-vf", f"scale={scale}:-2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "30", "-preset", "veryfast",
            "-movflags", "+faststart", "-an",
            str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    list_path.unlink(missing_ok=True)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--scale", default="640", help="largeur de la video encodee")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("[video] ffmpeg introuvable : aucune video ne sera produite")
        return 0

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"[video] {runs_dir} absent : rien a assembler")
        return 0

    goals = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    if not goals:
        print("[video] aucun dossier de goal sous " + str(runs_dir))
        return 0

    made = 0
    for goal_dir in goals:
        entries = numbered(goal_dir / "frames")
        if len(entries) < 2:
            print(f"[video] {goal_dir.name} : moins de 2 frames, video ignoree")
            continue
        out_path = goal_dir / f"{goal_dir.name}.mp4"
        result = encode(entries, out_path, args.scale)
        if result.returncode != 0:
            print(f"[video] {goal_dir.name} : ECHEC ffmpeg\n{result.stderr.strip()[-800:]}")
            continue
        size_kb = out_path.stat().st_size // 1024
        span = (entries[-1][0] - entries[0][0]) / 1000.0
        print(f"[video] {goal_dir.name} : {len(entries)} frames / {span:.1f}s -> {out_path} ({size_kb} Ko)")
        made += 1

    print(f"[video] {made} video(s) produite(s) sur {len(goals)} goal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
