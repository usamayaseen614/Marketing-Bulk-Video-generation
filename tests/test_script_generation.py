"""Generated voiceover scripts: one per rendered video.

Gemini and the speech engine are stubbed — what is under test is the dealing:

  * every video whose row left Voiceover and Screen_Text blank gets its own
    script, stored on its item, and no two share one
  * a typed Voiceover still wins, and a Screen_Text-only row stays silent
  * the stage synthesizes exactly what the renderer will look up, because both
    go through with_item_script
  * a resumed job does not pay Gemini twice
  * a failed generation is a warning, never an exception
"""
import os, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="scriptgen_")
os.environ["BVG_GCP_PROJECT"] = "fake-project"
os.environ["BVG_CAPTION_MAX_ATTEMPTS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import batching
from captions import pool as gemini
from jobs import store
from jobs.runners import voice as voice_stage
from speech import pool as script_pool
from speech import synth
from video_generator import RowSpec

store.init_db()

# ---- stubs ------------------------------------------------------------------
calls = {"gemini": 0, "synth": []}


def fake_generate(client, prompt, schema, model=None, attempts=None):
    calls["gemini"] += 1
    assert prompt.startswith("Sell coffee."), prompt
    n = calls["gemini"]
    return {"scripts": [f'"Script {n}-{i} ☕"' for i in range(25)]}


gemini._generate = fake_generate
gemini._client = lambda: None
gemini._thread_client = lambda: None
synth.available = lambda engine=None: (True, "")
synth.configure_threads = lambda *a, **k: None


def fake_synthesize(text, voice, speed, cache, lang, lead_in, engine):
    calls["synth"].append((text, voice))
    return {"wav": "x.wav", "duration": 1.0, "words": []}


synth.synthesize = fake_synthesize
voice_stage.ProcessPoolExecutor = ThreadPoolExecutor

VOICES = ["af_heart", "am_adam"]
N_BATCHES, N_ROWS = 4, 3


def make_job(prompt="Sell coffee."):
    params = {"script_source": "generate", "script_prompt": prompt,
              "render_config": {"voice_enabled": True, "voice_set": VOICES}}
    job_id = store.create_job(kind=store.KIND_RENDER, params=params)
    store.make_job_dirs(job_id)
    pd.DataFrame({
        "Headline": ["a", "b", "c"],
        "Voiceover": ["", "Typed script", ""],
        "Screen_Text": ["", "", "Silent caption"],
    }).to_excel(store.assets_dir(job_id) / "input.xlsx", index=False)
    slots = batching.plan_render(N_BATCHES, N_ROWS)
    store.add_items(job_id, [
        {"idx": batching.item_index(s.batch, s.row, N_ROWS), "meta": {"caption": "c"}}
        for s in slots])
    return {"id": job_id, "params": params}, slots


# ---- first run --------------------------------------------------------------
job, slots = make_job()
result = voice_stage.run_stage(job, job["params"], slots, N_ROWS)
print("result:", result)
assert "warning" not in result, result
metas = {i["idx"]: i["meta"] for i in store.list_items(job["id"])}

generated = [metas[batching.item_index(b, 1, N_ROWS)] for b in range(1, N_BATCHES + 1)]
scripts = [m["voiceover"] for m in generated]
assert len(set(scripts)) == N_BATCHES, scripts
assert all(s.startswith("Script") and "☕" not in s and '"' not in s
           for s in scripts), scripts
assert all(m["caption"] == "c" for m in generated), "naming meta was clobbered"
assert {m["voice"] for m in generated} == set(VOICES), generated
for b in range(1, N_BATCHES + 1):
    assert "voiceover" not in metas[batching.item_index(b, 2, N_ROWS)]
    assert "voiceover" not in metas[batching.item_index(b, 3, N_ROWS)]

# Synthesized exactly what the renderer will read: the 4 generated scripts plus
# the typed one (once — it is the same words in every batch), never the
# Screen_Text-only row.
frame = pd.read_excel(store.assets_dir(job["id"]) / "input.xlsx")
expected = set()
for s in slots:
    meta = metas[batching.item_index(s.batch, s.row, N_ROWS)]
    spec = RowSpec.from_row(script_pool.with_item_script(frame.iloc[s.row - 1], meta))
    if spec.voiceover:
        expected.add(spec.voiceover)
assert expected == set(scripts) | {"Typed script"}, expected
assert {t for t, _v in calls["synth"]} == expected, calls["synth"]
assert len(calls["synth"]) == 5, calls["synth"]

# ---- resume: nothing new to write -------------------------------------------
before = calls["gemini"]
voice_stage.run_stage(job, job["params"], slots, N_ROWS)
assert calls["gemini"] == before, "a resumed job paid Gemini again"
again = {i["idx"]: i["meta"] for i in store.list_items(job["id"])}
assert again == metas, "a resumed job changed its scripts"

# ---- failure is a warning, not an exception ---------------------------------
def denied(*a, **k):
    raise PermissionError("403 permission denied")


gemini._generate = denied
job2, slots2 = make_job()
result2 = voice_stage.run_stage(job2, job2["params"], slots2, N_ROWS)
print("failed result:", result2)
assert "Script generation failed" in result2.get("warning", ""), result2
assert not any("voiceover" in i["meta"] for i in store.list_items(job2["id"]))

job3, slots3 = make_job(prompt="  ")
result3 = voice_stage.run_stage(job3, job3["params"], slots3, N_ROWS)
assert "no prompt" in result3.get("warning", ""), result3

# ---- resume after a failed generation: finished videos are left alone -------
# job2 rendered silent. Batch 1 finished before the worker died; Gemini works now.
gemini._generate = fake_generate
for r in range(1, N_ROWS + 1):
    store.update_item(job2["id"], batching.item_index(1, r, N_ROWS),
                      render_status=store.ITEM_DONE)
calls["synth"].clear()
voice_stage.run_stage(job2, job2["params"], slots2, N_ROWS)
metas2 = {i["idx"]: i["meta"] for i in store.list_items(job2["id"])}
assert "voiceover" not in metas2[batching.item_index(1, 1, N_ROWS)], \
    "a finished silent video was given a script it will never speak"
assert all("voiceover" in metas2[batching.item_index(b, 1, N_ROWS)]
           for b in range(2, N_BATCHES + 1)), metas2
assert len(calls["synth"]) == (N_BATCHES - 1) + 1, calls["synth"]

# ---- no speech engine here: scripts are still written, for timed captions ---
synth.available = lambda engine=None: (False, "not installed")
calls["synth"].clear()
job4, slots4 = make_job()
result4 = voice_stage.run_stage(job4, job4["params"], slots4, N_ROWS)
assert result4.get("skipped") == "not installed", result4
assert result4.get("generated_scripts") == N_BATCHES, result4
assert not calls["synth"], calls["synth"]

# ---- a later round that fails whole keeps what earlier rounds paid for ------
def first_call_only(client, prompt, schema, model=None, attempts=None):
    calls["gemini"] += 1
    if calls["gemini"] > first:
        raise PermissionError("429 quota")
    return {"scripts": [f"Kept {i}" for i in range(25)]}


first = calls["gemini"] + 1
gemini._generate = first_call_only
kept = gemini.generate_scripts("Sell coffee.", 60)
assert kept == [f"Kept {i}" for i in range(25)], kept

# ---- only a quote pair wrapping the whole script is removed -----------------
assert gemini._clean_script('"Wrapped whole"') == "Wrapped whole"
quoted = '"Best purchase ever." That is what Dana said. Try it and say "wow"'
assert gemini._clean_script(quoted) == quoted
assert gemini._clean_script("Dinner at the Smiths'") == "Dinner at the Smiths'"

print("OK — script generation")
