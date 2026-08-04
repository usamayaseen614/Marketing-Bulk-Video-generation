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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Rendering engine + UI
COPY video_generator.py app.py preview_editor.py ./
# Automation layer
COPY config.py workspace.py results.py ui_common.py ./
COPY jobs/ jobs/
COPY integrations/ integrations/
COPY captions/ captions/
COPY scrapers/ scrapers/
COPY pages/ pages/
COPY fonts/ fonts/
# Toolbar settings (hides the Deploy button and options menu).
COPY .streamlit/ .streamlit/
COPY supervisord.conf /etc/supervisor/conf.d/app.conf
# Streamlit's static serving requires ./static to exist at startup; large
# result ZIPs are streamed from here (see ui_common.offer_zip_download).
RUN mkdir -p static

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
