"""Blocking quality gates.

These replace the toddler-safety gates from the kids design. The ordering is
deliberate: accuracy gates run before aesthetic ones, because a factually wrong
episode is unshippable no matter how it looks.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(slots=True)
class GateResult:
    name: str
    passed: bool
    blocking: bool = True
    detail: str = ""
    findings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL" if self.blocking else "WARN"


def _load(episode_dir: Path) -> tuple[dict, dict]:
    script = yaml.safe_load((episode_dir / "script.yaml").read_text())
    root = episode_dir.parents[1]
    dossier = yaml.safe_load((root / script["dossier"]).read_text())
    return script, dossier


def gate_traceability(episode_dir: Path) -> GateResult:
    """Every beat's assertions must cite a dossier claim that is cleared for animation."""
    script, dossier = _load(episode_dir)
    claims = dossier["claims"]
    findings: list[str] = []
    for beat in script["beats"]:
        if beat["scene"] == "card":
            continue
        if not beat["cites"]:
            findings.append(f"{beat['id']}: asserts on screen but cites no claim")
            continue
        for cid in beat["cites"]:
            if cid not in claims:
                findings.append(f"{beat['id']}: cites unknown claim {cid}")
            elif not claims[cid]["animatable"]:
                findings.append(
                    f"{beat['id']}: cites {cid}, which the dossier marks NOT animatable"
                )
    return GateResult("traceability", not findings, True,
                      f"{len(script['beats'])} beats checked", findings)


def gate_source_tier(episode_dir: Path) -> GateResult:
    """No animated claim may rest solely on a source below the bible's minimum tier."""
    script, dossier = _load(episode_dir)
    root = episode_dir.parents[1]
    bible = yaml.safe_load((root / "bible" / "visual_grammar.yaml").read_text())
    min_tier = bible["accuracy"]["min_source_tier"]
    tiers = {k: v["tier"] for k, v in dossier["sources"].items()}
    cited = {c for b in script["beats"] for c in b["cites"]}
    findings = []
    for cid in sorted(cited):
        claim = dossier["claims"].get(cid)
        if not claim:
            continue
        best = min((tiers.get(s, 99) for s in claim["sources"]), default=99)
        if best > min_tier:
            findings.append(f"{cid}: best source tier {best} exceeds limit {min_tier}")
    return GateResult("source_tier", not findings, True,
                      f"{len(cited)} cited claims, min_tier={min_tier}", findings)


def gate_pacing(episode_dir: Path) -> GateResult:
    """Inverted from the kids design: a CEILING on static time, plus a floor on shot length."""
    script, dossier = _load(episode_dir)
    root = episode_dir.parents[1]
    bible = yaml.safe_load((root / "bible" / "visual_grammar.yaml").read_text())
    pac = bible["pacing"]
    findings = []
    total = 0.0
    for beat in script["beats"]:
        total += beat["seconds"]
        if beat["seconds"] < pac["min_shot_seconds"]:
            findings.append(f"{beat['id']}: {beat['seconds']}s below floor {pac['min_shot_seconds']}s")
    lo, hi = pac["target_total_seconds"]
    if not lo <= total <= hi:
        findings.append(f"total {total}s outside target {lo}-{hi}s")
    return GateResult("pacing", not findings, True, f"total={total}s", findings)


def gate_rendered_numbers(episode_dir: Path) -> GateResult:
    """On-screen quantities must trace to the dossier; constrained claims must not be violated.

    The zipper dossier forbids showing a numeric pitch offset (C4) because the patent
    gives none, so any bare number in beat copy is treated as suspect.
    """
    import re

    script, dossier = _load(episode_dir)
    allowed = {"1,219,881", "1917"}
    findings = []
    for beat in script["beats"]:
        text = f"{beat.get('text_main','')} {beat.get('text_sub','')}"
        for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
            if tok not in allowed:
                findings.append(f"{beat['id']}: unverified on-screen number '{tok}'")
    return GateResult("rendered_numbers", not findings, True,
                      f"allowlist={sorted(allowed)}", findings)


def gate_loudness(video: Path) -> GateResult:
    """Integrated loudness must sit at the bible target. Absent audio is reported, not passed."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    streams = json.loads(probe.stdout or "{}").get("streams", [])
    if not streams:
        return GateResult("loudness", False, blocking=False,
                          detail="no audio stream present — VO/music stage not yet run")
    res = subprocess.run(
        ["ffmpeg", "-nostats", "-i", str(video), "-filter:a", "loudnorm=print_format=json",
         "-f", "null", "-"], capture_output=True, text=True,
    )
    blob = res.stderr[res.stderr.rfind("{"): res.stderr.rfind("}") + 1]
    data = json.loads(blob)
    lufs = float(data["input_i"])
    ok = abs(lufs + 14.0) <= 1.0
    return GateResult("loudness", ok, True, f"integrated={lufs:.1f} LUFS (target -14.0)")


def run_all(episode_dir: Path, video: Path | None = None) -> list[GateResult]:
    results = [
        gate_traceability(episode_dir),
        gate_source_tier(episode_dir),
        gate_pacing(episode_dir),
        gate_rendered_numbers(episode_dir),
        gate_chapters(episode_dir),
    ]
    if video and video.exists():
        results.append(gate_loudness(video))
    return results


def gate_chapters(episode_dir: Path) -> GateResult:
    """YouTube chapter rules: first must be 00:00, at least three, each >= 10s.

    Violating any of them does not error -- YouTube silently drops chapters for the
    whole video, which is invisible unless something checks.
    """
    import re

    meta = yaml.safe_load((episode_dir / "metadata.yaml").read_text())
    script = yaml.safe_load((episode_dir / "script.yaml").read_text())
    total = sum(b["seconds"] for b in script["beats"])

    stamps: list[int] = []
    for line in meta["description"].splitlines():
        m = re.match(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s+\S", line)
        if m:
            h, mnt, sec = (int(x) if x else 0 for x in m.groups())
            stamps.append(h * 3600 + mnt * 60 + sec)

    findings = []
    if not stamps:
        return GateResult("chapters", True, False, "no chapters declared")
    if stamps[0] != 0:
        findings.append(f"first chapter at {stamps[0]}s, must be 00:00")
    if len(stamps) < 3:
        findings.append(f"{len(stamps)} chapters, YouTube requires at least 3")
    for a, b in zip(stamps, stamps[1:] + [int(total)]):
        if b - a < 10:
            findings.append(f"chapter at {a}s runs {b - a}s, under the 10s minimum")
    if stamps[-1] >= total:
        findings.append(f"last chapter at {stamps[-1]}s is beyond runtime {total}s")
    return GateResult("chapters", not findings, True, f"{len(stamps)} chapters", findings)
