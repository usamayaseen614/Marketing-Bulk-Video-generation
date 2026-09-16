# Bulk Marketing Video Generator — production image
#
# ffmpeg comes from apt (the app prefers a PATH ffmpeg over the bundled
# imageio-ffmpeg binary); fonts-dejavu-core provides DejaVuSans-Bold, which is
# in the app's font fallback chain, so text rendering works out of the box.
#
# The image runs TWO processes under supervisord: the Streamlit UI and the
# background job worker. Clicking Generate now queues a job and returns
# immediately, so something has to be running to pick that job up.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core supervisor \
       espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch FIRST, and from the CPU index, or the voiceover dependency drags in
# the default CUDA wheel: ~2.5 GB of nvidia-* libraries that cannot be used
# here at all, because every machine this runs on encodes with libx264 on the
# CPU and has no GPU. Installing it up front leaves the requirements install
# below seeing torch already satisfied.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.5"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the Kokoro voice model into the image. Fetching it on first use would
# mean a 16,000-video job racing sixteen render threads for the same file, and
# a render that fails whenever the network does. The weights come from Hugging
# Face rather than PyPI, so pip cannot do it. Non-fatal: speech/synth.py falls
# back to downloading on demand, and to 'voiceover unavailable' after that.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['*.pth','*.json','voices/*']); \
print('kokoro weights cached')" \
    || echo "WARNING: Kokoro weights not pre-cached; the first voiceover job fetches them"

# The same treatment for the other engine, for the same reason. Pocket TTS
# fetches its weights inside TTSModel.load_model() rather than from one known
# repo, so the warm-up is a real load — which also proves the install works at
# build time instead of at 3am on the first narrated batch. ~100M parameters,
# a little MORE than Kokoro's 82M. Non-fatal in exactly the same way:
# speech/synth.py falls back to downloading on demand, and to "not installed
# here" after that.
#
# Every offered voice, not just the default, because Kokoro's line above bakes
# `voices/*` and a batch that rotates through four narrators would otherwise
# fetch three of them mid-render. Each voice is its own small download from
# Hugging Face (the model itself is fetched once, by load_model above).
#
# The list is duplicated from speech/synth.py's POCKET_VOICES, which is the
# source of truth, because the app's own modules are COPYed in below this layer
# — importing it here would tie the weight cache to every code change and undo
# the layer caching this ordering exists for. Keep the two in step.
RUN python -c "\
from pocket_tts_timestamped import TTSModel; \
m = TTSModel.load_model(); \
voices = 'alba anna azelma bill_boerst caro_davy charles cosette eponine eve \
fantine george jane javert jean marius mary michael paul peter_yearsley \
stuart_bell vera'.split(); \
[m.get_state_for_audio_prompt(v) for v in voices]; \
print('pocket-tts cached:', len(voices), 'voices at', m.sample_rate, 'Hz')" \
    || echo "WARNING: Pocket TTS weights not pre-cached; the first voiceover job fetches them"

# Every top-level module, as a glob rather than a hand-maintained list.
# Listing them individually is how batching.py got left out of the image: it
# imported fine locally and every render on the VM died with
# "No module named 'batching'". .dockerignore is what excludes the ones that
# should not ship.
COPY *.py ./
COPY jobs/ jobs/
COPY integrations/ integrations/
COPY captions/ captions/
COPY speech/ speech/
COPY scrapers/ scrapers/
COPY pages/ pages/
COPY fonts/ fonts/
# Operator scripts, run with `docker exec` — tools/zip_drive_tk.py repacks
# folders that are already in Drive, which is work done ON the VM (its link to
# Google is the fast one) but not through the UI.
COPY tools/ tools/
# Toolbar settings (hides the Deploy button and options menu).
COPY .streamlit/ .streamlit/
COPY supervisord.conf /etc/supervisor/conf.d/app.conf
# Streamlit's static serving requires ./static to exist at startup; large
# result ZIPs are streamed from here (see ui_common.offer_zip_download).
RUN mkdir -p static

# Fail the BUILD if anything the worker needs is missing from the image.
#
# Without this, a module left out of the COPY above only shows up when a real
# job runs — after a batch has been submitted, on the VM, minutes or hours
# later. Importing every non-UI module here turns that into an immediate build
# failure. app.py and pages/ are excluded because importing them executes
# Streamlit page code.
RUN python -c "\
import importlib, sys; \
mods = ['config','workspace','results','batching','packing','video_generator', \
        'text_grids', \
        'preview_editor','jobs.store','jobs.worker','jobs.runners.render', \
        'jobs.runners.scrape','jobs.runners.captions','integrations.drive', \
        'integrations.mailer','captions.naming','captions.pool', \
        'captions.assign','scrapers.tiktok','tools.zip_drive_tk', \
        'jobs.runners.voice','speech.beats','speech.pool','speech.synth']; \
[importlib.import_module(m) for m in mods]; \
print('import check OK:', len(mods), 'modules')"

# Job records, staged uploads and rendered videos live here. This MUST be a
# mounted volume in production — anything on the container filesystem is lost
# on `gcloud compute instances update-container`, which would strand queued
# jobs and discard finished videos that hadn't reached Drive yet.
ENV BVG_JOBS_ROOT=/data/jobs
VOLUME ["/data"]
RUN mkdir -p /data/jobs

EXPOSE 8501

# Checks the UI is serving AND that the worker process is alive — a container
# where only Streamlit survived would accept batches and never run them.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" \
        && supervisorctl -c /etc/supervisor/supervisord.conf status worker | grep -q RUNNING \
        || exit 1

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
