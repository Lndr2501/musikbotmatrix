FROM python:3.12-slim

# ffmpeg wird nur fuer yt-dlp-Merge benoetigt (Dekodierung macht PyAV im Bot)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Stimme fuer die Ansage "Jetzt kommt ..." (Piper, ~63 MB)
RUN mkdir -p /opt/piper && python -c "import urllib.request as u; b='https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx'; [u.urlretrieve(b + s, '/opt/piper/de_DE-thorsten-medium.onnx' + s) for s in ('', '.json')]" \
    && echo "fd270eb3d1ebf91d763f8a0f949b1b1bebdfefaea18c3aaf8577dd34536e064f  /opt/piper/de_DE-thorsten-medium.onnx" | sha256sum -c -

WORKDIR /app
COPY pyproject.toml ./
COPY musikbot ./musikbot

# WITH_STT=false spart ~250 MB, wenn keine eigene Spracherkennung (stt_server) gebraucht wird
ARG WITH_STT=true
RUN if [ "$WITH_STT" = "true" ]; then pip install --no-cache-dir ".[stt]"; else pip install --no-cache-dir .; fi

# Non-root; /data ist das Volume fuer Cache/Bibliothek/Matrix-Store
RUN useradd --create-home --uid 10000 musikbot \
    && mkdir -p /data && chown musikbot:musikbot /data
USER musikbot
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "musikbot"]
