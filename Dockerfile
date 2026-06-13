# Bulk Marketing Video Generator — production image
#
# ffmpeg comes from apt (the app prefers a PATH ffmpeg over the bundled
# imageio-ffmpeg binary); fonts-dejavu-core provides DejaVuSans-Bold, which is
# in the app's font fallback chain, so text rendering works out of the box.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY video_generator.py app.py preview_editor.py ./
# Toolbar settings (hides the Deploy button and options menu).
COPY .streamlit/ .streamlit/
# Streamlit's static serving requires ./static to exist at startup; large
# result ZIPs are streamed from here (see offer_download in app.py).
RUN mkdir -p static

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

# maxUploadSize is in MB — real background ZIPs easily exceed Streamlit's
# 200 MB default. enableStaticServing powers the large-ZIP download path.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableStaticServing=true", \
     "--server.maxUploadSize=2000"]
