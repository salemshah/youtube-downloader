# Use slim base to keep image small
FROM python:3.12-slim

# Install system deps: ffmpeg for merging, curl for debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m appuser
WORKDIR /app

# Copy & install Python deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# yt-dlp is a Python package (fast updates)
RUN pip install --no-cache-dir yt-dlp

# Copy the rest of the project
COPY . /app

# Make folders that your code writes to
RUN mkdir -p downloads staticfiles && chown -R appuser:appuser /app
USER appuser

# Django runs on 8000 by default
EXPOSE 8000

# Default command: dev server (hot reload works with bind mount)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
