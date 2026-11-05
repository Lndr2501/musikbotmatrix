FROM python:3.12-slim

# ffmpeg wird nur fuer yt-dlp-Merge benoetigt (Dekodierung macht PyAV im Bot)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY musikbot ./musikbot

RUN pip install --no-cache-dir .

# Non-root; /data ist das Volume fuer Cache/Bibliothek/Matrix-Store
RUN useradd --create-home --uid 10000 musikbot \
    && mkdir -p /data && chown musikbot:musikbot /data
USER musikbot
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "musikbot"]
