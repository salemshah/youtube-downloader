import os, uuid, shutil, subprocess, sys
from django.http import FileResponse
from rest_framework.decorators import api_view
from rest_framework import status
from rest_framework.response import Response

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")

@api_view(['POST'])
def download_yt(request):
    url = request.data.get('url')
    if not url:
        return Response({"error": "Missing 'url'"}, status=status.HTTP_400_BAD_REQUEST)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not shutil.which("ffmpeg"):
        return Response({"error": "ffmpeg not found in PATH"}, status=500)
    if not shutil.which("yt-dlp"):
        return Response({"error": "yt-dlp not found in PATH"}, status=500)

    # make a deterministic temp filename (avoid guessing the yt-dlp title)
    tmp_id = uuid.uuid4().hex
    out_path = os.path.join(DOWNLOAD_DIR, f"{tmp_id}.mp4")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", out_path,
        url
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        return Response({"error": "Download failed", "detail": str(e)}, status=500)

    # stream file back to client as a download
    resp = FileResponse(open(out_path, "rb"), as_attachment=True, filename=os.path.basename(out_path))
    # (optional) delete after sending? risky while streaming; safer to clean with a cron/job later
    return resp
