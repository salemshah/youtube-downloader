import os, uuid, shutil, subprocess
from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view
from rest_framework import status
from rest_framework.response import Response

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

def _stream_file_then_delete(path, chunk_size=1024 * 64):
    """Yield file chunks; always delete the file when done or if client disconnects."""
    f = open(path, "rb")
    try:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            f.close()
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

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

    tmp_id = uuid.uuid4().hex
    out_path = os.path.join(DOWNLOAD_DIR, f"{tmp_id}.mp4")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", out_path,
        url,
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        return Response({"error": "Download failed", "detail": str(e)}, status=500)

    # Prepare headers BEFORE streaming/deleting
    filename = os.path.basename(out_path)
    try:
        file_size = os.path.getsize(out_path)
    except OSError:
        return Response({"error": "File not found after download"}, status=500)

    resp = StreamingHttpResponse(_stream_file_then_delete(out_path), content_type="video/mp4")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp["Content-Length"] = str(file_size)  # helps browsers show progress
    return resp
