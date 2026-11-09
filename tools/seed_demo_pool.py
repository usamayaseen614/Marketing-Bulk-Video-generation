"""
tools/seed_demo_pool.py — a fake caption pool, for testing without Gemini.

Caption-based filenames are the part of the pipeline most worth eyeballing, but
generating a real pool needs Vertex AI configured. This writes a small pool of
obviously-fake captions so the naming, manifests and the 90-character rule can
all be exercised end to end first.

    python tools/seed_demo_pool.py

Generate a real pool from the Setup page when you are ready; doing so replaces
this one (only the newest pool is active).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from jobs import store  # noqa: E402

# Deliberately varied: short, long, punctuation, emoji (which must be stripped),
# and one that would blow the 90-char cap on its own.
CAPTIONS = [
    "This is the one thing nobody tells you about it",
    "Stop scrolling — this actually works",
    "POV: you finally found the good stuff",
    "Why is nobody doing this?!",
    "The 3-second trick that changes everything",
    "I wish I had found this sooner 🔥",
    "Everyone keeps asking me where I got this so here it is once and for all",
    "Three reasons this is worth your time",
    "It took me way too long to figure this out",
    "Save this before it disappears",
    "The difference is honestly ridiculous",
    "Nobody believes me until they try it",
]

HASHTAG_SETS = [
    "#asmr #satisfying #fyp #viral #foryou",
    "#skincare #glowup #selfcare #routine #fyp",
    "#relaxing #calm #sleep #asmr #foryou",
    "#trending #musthave #tiktokmademebuyit #fyp",
    "#aesthetic #oddlysatisfying #viral #foryou",
]


def main() -> int:
    store.init_db()
    pool_id = store.save_pool(
        theme="demo pool (seeded locally — not from Gemini)",
        captions=CAPTIONS,
        hashtags=HASHTAG_SETS,
        model="seeded-by-hand",
    )
    combos = len(CAPTIONS) * len(HASHTAG_SETS)
    print(f"Seeded caption pool {pool_id}")
    print(f"  {len(CAPTIONS)} captions x {len(HASHTAG_SETS)} hashtag sets "
          f"= {combos} unique pairs")
    print(f"  stored in {config.DB_PATH}")
    print()
    print("This is now the ACTIVE pool. Rendered files will be named from these")
    print("captions. Generating a real pool on the Setup page replaces it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
