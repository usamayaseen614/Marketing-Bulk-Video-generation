"""
pages/3_Setup.py — what's configured, and whether it actually works.

Every integration here fails in a way that is invisible until the moment you
need it: a wrong app password only shows up when a batch finishes and no email
arrives; a service account that was never added to the Shared Drive only shows
up after 300 videos have been rendered. This page makes each one testable in
one click, and reports the specific cause rather than a stack trace.
"""

from __future__ import annotations

import streamlit as st

import config as settings
from jobs import store

st.set_page_config(page_title="Setup", page_icon="🔧", layout="wide")
store.init_db()

st.title("🔧 Setup & diagnostics")
st.caption(
    "Settings come from environment variables (or a `.env` file next to "
    "`app.py`). Nothing here is stored in the app — change the variables and "
    "restart the container."
)


def status_row(label: str, ok: bool, detail: str) -> None:
    icon = "✅" if ok else "⚠️"
    st.markdown(f"**{icon} {label}** — {detail}")


st.subheader("Current state")

status_row(
    "Job storage", True,
    f"`{settings.JOBS_ROOT}` · retention {settings.JOB_RETENTION_DAYS} days",
)
status_row(
    "Email", settings.mail_configured(),
    f"sending as `{settings.MAIL_FROM}` to `{', '.join(settings.MAIL_TO)}`"
    if settings.mail_configured()
    else "not configured — batches will finish silently",
)
status_row(
    "Google Drive", settings.drive_configured(),
    f"Shared Drive `{settings.DRIVE_SHARED_DRIVE_ID}`"
    if settings.drive_configured()
    else "not configured — videos stay on the VM and the ZIP is the only output",
)
status_row(
    "Gemini (captions)", settings.gemini_configured(),
    f"Vertex AI · project `{settings.GCP_PROJECT}` · {settings.VERTEX_LOCATION} "
    f"· {settings.GEMINI_POOL_MODEL}"
    if settings.gemini_configured()
    else "not configured — captions and hashtags won't be generated",
)

# The worker is the thing that actually does the work; if it isn't running,
# every submitted batch just sits in the queue looking healthy.
running = store.list_jobs(limit=5, statuses=[store.STATUS_RUNNING])
queued = store.list_jobs(limit=50, statuses=[store.STATUS_QUEUED])
if running:
    status_row("Worker", True, f"{len(running)} job(s) running")
elif queued:
    st.markdown(
        f"**⚠️ Worker** — {len(queued)} job(s) queued but nothing running. "
        "If this doesn't change within a few seconds the worker process is "
        "down. On the VM it runs alongside Streamlit in the same container "
        "(`python -m jobs.worker`)."
    )
else:
    status_row("Worker", True, "idle — nothing queued")

st.divider()

# --------------------------------------------------------------------------- email

st.subheader("📧 Email")

if not settings.mail_configured():
    st.info(
        "Set these to enable notifications:\n\n"
        "```\n"
        "BVG_SMTP_USER=notifications@yourcompany.com\n"
        "BVG_SMTP_PASSWORD=<16-character app password>\n"
        "BVG_MAIL_TO=you@yourcompany.com\n"
        "```\n"
        "The password is a Google **app password**, not the account password. "
        "Create one at myaccount.google.com → Security → 2-Step Verification → "
        "App passwords. The account needs 2-Step Verification switched on first."
    )

test_to = st.text_input(
    "Send a test email to", value=", ".join(settings.MAIL_TO),
    placeholder="you@yourcompany.com",
)
if st.button("Send test email", disabled=not settings.mail_configured()):
    from integrations import mailer
    with st.spinner("Sending…"):
        ok, message = mailer.send_test_email(test_to.strip() or None)
    (st.success if ok else st.error)(message)

st.divider()

# --------------------------------------------------------------------------- drive

st.subheader("📁 Google Drive")

if not settings.drive_configured():
    st.info(
        "Set the destination — **paste the folder URL straight from your "
        "browser**, the app works out the rest:\n\n"
        "```\n"
        "BVG_DRIVE_SHARED_DRIVE_ID=https://drive.google.com/drive/folders/1ab2...\n"
        "```\n"
        "**It must live in a Shared Drive.** A service account has no storage "
        "of its own, so a folder in someone's personal *My Drive* fails no "
        "matter how it is shared. The test button below says which you have."
    )
else:
    from integrations import drive as _drive
    _id = _drive.extract_id(settings.DRIVE_SHARED_DRIVE_ID)
    st.caption(f"Destination id: `{_id}`")
    if not _drive.looks_like_shared_drive(_id):
        st.warning(
            f"`{_id}` starts with `{_id[:1]}`, which means it's a **folder** "
            "rather than a Shared Drive itself. That's fine *if* the folder "
            "sits inside a Shared Drive — press *Test Drive access* to find "
            "out. Shared Drive ids start with `0A`.\n\n"
            "**How to check by eye:** open the folder in Drive and look at the "
            "left sidebar. If the path starts under *Shared drives*, you're "
            "good. If it starts under *My Drive*, uploads will fail."
        )

if st.button("Test Drive access", disabled=not settings.drive_configured()):
    from integrations import drive
    with st.spinner("Checking…"):
        ok, message = drive.check_access()
    (st.success if ok else st.error)(message)

# Jobs can carry their own destination now, so any link should be checkable
# before a multi-hour batch is pointed at it.
st.markdown("**Test a specific folder link**")
st.caption(
    "Generate and the Scraper both accept a per-job Drive link. Paste one here "
    "to check it first — this does a real upload and a real server-side copy, "
    "then removes what it made."
)
custom_link = st.text_input(
    "Folder link or ID", value="", label_visibility="collapsed",
    placeholder="https://drive.google.com/drive/folders/…",
)
if st.button("Test this link", disabled=not custom_link.strip()):
    from integrations import drive
    with st.spinner("Checking that link…"):
        ok, message = drive.check_access(target=custom_link.strip())
    (st.success if ok else st.error)(message)
    if ok:
        st.caption(
            "Paste this same link into the job's *Google Drive folder link* "
            "field to send that batch here."
        )

with st.expander("Drive setup steps"):
    st.markdown(
        """
1. **Create the Shared Drive** — drive.google.com → *Shared drives* in the left
   sidebar → **New**. If you have no *Shared drives* entry, the account is not
   on Google Workspace and this won't work; tell me and we'll pick another route.
2. **Find the service account's email.** In Cloud Console → IAM & Admin →
   Service Accounts on project `YOUR_PROJECT_ID`. It ends in
   `.iam.gserviceaccount.com`.
3. **Give it access.** Either route works, and they use different role names:
   - *Share the folder* (right-click the folder → Share) → role **Editor**.
     This dialog has no "Content Manager" option — Editor is the top role here.
   - *Or add it to the Shared Drive* (open the drive → Manage members) → role
     **Content Manager**.

   Viewer and Commenter are not enough either way.
4. **Grant the VM the Drive scope.** The VM's token only carries scopes it was
   created with, and `cloud-platform` does *not* include Drive. This needs the
   VM stopped.
5. **Set `BVG_DRIVE_SHARED_DRIVE_ID`** and restart the container, then press
   *Test Drive access* above.
        """
    )

st.divider()

# --------------------------------------------------------------------------- captions

st.subheader("✍️ Captions & hashtags")

pool = store.active_pool()
if pool:
    st.success(
        f"Active pool `{pool['id']}` — **{len(pool['captions']):,} captions x "
        f"{len(pool['hashtags']):,} hashtag sets = "
        f"{pool['combinations']:,} unique pairs**. "
        f"{pool['cursor']:,} used so far "
        f"({pool['combinations'] - pool['cursor']:,} left before any repeat)."
    )
    st.caption(f"Theme: {pool.get('theme')}")
    with st.expander("Sample from this pool"):
        for caption, tags in zip(pool["captions"][:5], pool["hashtags"][:5]):
            st.markdown(f"- **{caption}**  \n  `{tags}`")
else:
    st.info(
        "No caption pool yet. Without one, videos are named from their "
        "`Headline` as before — captions are an addition, not a requirement."
    )

st.caption(
    "Captions are generic and built around a theme you choose. A pool is "
    "generated occasionally and recombined per batch, so there is no model call "
    "per video — 2,000 captions x 500 hashtag sets is a million unique pairs, "
    "about a year and a half of output at 2,000 videos a day."
)

theme = st.text_input(
    "Caption theme",
    value=settings.CAPTION_THEME,
    placeholder="e.g. satisfying ASMR clips promoting a skincare brand",
    help="What the captions should be about. This is the single biggest lever "
         "on their quality — be specific about the product and the audience.",
)
col_c, col_h = st.columns(2)
n_captions = col_c.number_input("Captions to generate", 50, 5000,
                                settings.CAPTION_POOL_SIZE, 50)
n_hashtags = col_h.number_input("Hashtag sets to generate", 25, 2000,
                                settings.HASHTAG_POOL_SIZE, 25)
st.caption(
    f"→ {int(n_captions) * int(n_hashtags):,} unique pairs. "
    f"Roughly ${int(n_captions) / 2000 * 2:.2f} of Gemini usage at current "
    f"{settings.GEMINI_POOL_MODEL} pricing."
)

if st.button("Generate a new pool", type="primary",
             disabled=not (settings.gemini_configured() and theme.strip())):
    job_id = store.new_job_id()
    store.make_job_dirs(job_id)
    store.create_job(
        kind=store.KIND_CAPTIONS,
        params={"theme": theme.strip(), "captions": int(n_captions),
                "hashtags": int(n_hashtags)},
        label=f"caption-pool-{theme.strip()[:30]}",
        notify_email=", ".join(settings.MAIL_TO),
        job_id=job_id,
    )
    st.success(
        "Queued. Generating a full pool takes a few minutes — track it on the "
        "Jobs page. The new pool becomes active automatically when it finishes."
    )

if st.button("Test Vertex AI connection", disabled=not settings.gemini_configured()):
    from captions import pool as pool_module
    with st.spinner("Asking Gemini for two sample captions…"):
        ok, message = pool_module.check_access()
    (st.success if ok else st.error)(message)

if not settings.gemini_configured():
    st.info(
        "Set the project to enable caption generation:\n\n"
        "```\n"
        "BVG_GCP_PROJECT=YOUR_PROJECT_ID\n"
        "BVG_CAPTION_THEME=<what your captions are about>\n"
        "```\n"
        "Vertex AI uses the same service account the VM already has, so there "
        "is no separate API key. Enable the Vertex AI API on the project and "
        "give the service account the *Vertex AI User* role."
    )

st.divider()

# --------------------------------------------------------------------------- vars

st.subheader("All settings")
with st.expander("Environment variables this app reads"):
    st.code(
        f"""# storage
BVG_JOBS_ROOT={settings.JOBS_ROOT}
BVG_JOB_RETENTION_DAYS={settings.JOB_RETENTION_DAYS}

# email
BVG_SMTP_HOST={settings.SMTP_HOST}
BVG_SMTP_PORT={settings.SMTP_PORT}
BVG_SMTP_USER={settings.SMTP_USER or '<not set>'}
BVG_SMTP_PASSWORD={'<set>' if settings.SMTP_PASSWORD else '<not set>'}
BVG_MAIL_FROM={settings.MAIL_FROM or '<not set>'}
BVG_MAIL_TO={', '.join(settings.MAIL_TO) or '<not set>'}

# drive
BVG_DRIVE_SHARED_DRIVE_ID={settings.DRIVE_SHARED_DRIVE_ID or '<not set>'}
BVG_DRIVE_ROOT_FOLDER_ID={settings.DRIVE_ROOT_FOLDER_ID or '<Shared Drive root>'}
BVG_DRIVE_UPLOAD_CONCURRENCY={settings.DRIVE_UPLOAD_CONCURRENCY}

# gemini (captions)
BVG_GCP_PROJECT={settings.GCP_PROJECT or '<not set>'}
BVG_VERTEX_LOCATION={settings.VERTEX_LOCATION}
BVG_GEMINI_POOL_MODEL={settings.GEMINI_POOL_MODEL}
BVG_CAPTION_THEME={settings.CAPTION_THEME or '<not set>'}

# scraper
BVG_SCRAPE_MAX_VIDEOS={settings.SCRAPE_MAX_VIDEOS}
BVG_SCRAPE_BATCH_SIZE={settings.SCRAPE_BATCH_SIZE}
BVG_SCRAPE_SLOTS={settings.SCRAPE_SLOTS}
BVG_SCRAPE_TRIM_START={settings.SCRAPE_TRIM_START}
BVG_SCRAPE_TRIM_DURATION={settings.SCRAPE_TRIM_DURATION}
BVG_SCRAPE_COOKIES_FILE={settings.SCRAPE_COOKIES_FILE or '<not set>'}""",
        language="bash",
    )
