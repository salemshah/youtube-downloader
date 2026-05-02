FROM python:3.12-slim

# ffmpeg is required for merging video + audio streams (1080p+)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# yt-dlp installed separately for easy independent updates
RUN pip install --no-cache-dir yt-dlp

COPY . /app

# downloads/ is used for temporary merge files — cleaned up after each response
RUN mkdir -p downloads staticfiles && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
