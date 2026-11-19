# explainer_studio

Agentic pipeline for animated mechanism explainers. Research-led: a locked, cited
dossier gates everything downstream, because a script can be fluent and confidently
wrong, and the animation will render the error faithfully.

## Why it is shaped this way

**The orchestrator is a state machine, not a conversational agent.** Control flow lives
in `orchestrator/pipeline.py`; the LLM never decides what runs next. A long creative
pipeline driven by one agent loses coherence and burns context, and cannot resume at the
stage that failed.

**Agents do judgment; code does everything else.** Research, scripting, scene authoring
and review are agent work. Rendering, assembly, QC and upload are deterministic code.

**Style is the IP.** With no recurring characters, `bible/visual_grammar.yaml` carries
the channel's identity: colour semantics, cutaway and camera grammar, typography, pacing.
Agents read it and may not deviate.

**Frame N is a pure function of N.** No RAF, no clock reads, no RNG in a scene, so an
episode is reproducible from script + dossier alone.

## Pipeline

```
topic-scout -> research-dossier -> [LOCK] -> script-writer -> fact-checker
                                                                   |
            publish (private) <- QC gates <- assemble <- render <--+
```

## Quality gates (`qc/gates.py`)

| Gate | Blocking | Checks |
|---|---|---|
| `traceability` | yes | every asserting beat cites an animatable dossier claim |
| `source_tier` | yes | no animated claim rests solely on a below-threshold source |
| `pacing` | yes | shot-length floor and episode total against the bible |
| `rendered_numbers` | yes | on-screen quantities trace to the dossier |
| `loudness` | yes | integrated LUFS at target; **reports, never passes, when audio is absent** |

Gates are negative-tested: planting a citation to a rejected claim, an unsourced number
and an under-length shot trips traceability, source_tier, rendered_numbers and pacing.

## Run

```bash
pip install -r requirements.txt          # node + playwright + ffmpeg already required
python orchestrator/pipeline.py zipper-slider
python orchestrator/pipeline.py zipper-slider --skip-render   # re-assemble + re-QC only
```

## Render backend

`render/web_backend.py` drives Chromium through Node Playwright and screenshots exact
frame intervals. It sits behind the `FrameSource` protocol in `render/backend.py`, so a
Manim backend can replace it on a host with cairo/pango without touching callers.
Manim is the better tool for mathematical motion; it is not used here only because this
container has no pangocairo.

## Publishing

`publish/youtube.py` uploads **private** and declares `selfDeclaredMadeForKids: False`
explicitly. The private upload is the human review gate. Nothing in this repo flips a
video public.

Google's YouTube Data API revision history (2026-11-02) reversed the 2020 rule locking
unverified-project uploads to private permanently. Confirm that reached your project
before relying on it.

## Known gaps

- **No voice-over or music.** The episode is silent with on-screen typography; the
  loudness gate correctly reports the missing audio stream rather than passing.
- `plausibility-review` and `fact-checker` are written as contracts, not yet wired into
  `pipeline.py` as automated calls.
- `shorts_cutdown` takes a hardcoded start time; picking the most satisfying reveal
  should itself be an agent decision.
