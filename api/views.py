import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import urllib.parse
from urllib.parse import urlparse

import redis as redis_lib
from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view
from rest_framework import status
from rest_framework.response import Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ALLOWED_DOMAINS = {
    'youtube.com', 'www.youtube.com',
    'youtu.be',    'www.youtu.be',
    'm.youtube.com', 'music.youtube.com',
}

MAX_CONCURRENT_DOWNLOADS = 2
DOWNLOAD_TIMEOUT         = 600           # 10 minutes
MAX_DURATION_SECONDS     = 30 * 60       # 30 minutes
MAX_FILESIZE_BYTES       = 1_073_741_824 # 1 GB

# General rate limit (extract endpoint) — in-memory, per worker
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW   = 60   # seconds
_rate_store: dict[str, list[float]] = {}

# HD-specific rate limit — Redis-backed, truly global
HD_RATE_LIMIT  = 3    # max HD downloads per IP
HD_RATE_WINDOW = 600  # per 10 minutes

# Redis key namespaces
_SLOT_KEY        = 'hd:slots'          # sorted-set of active slot IDs
_HD_RATE_PREFIX  = 'hd:rate:'          # {ip} → request count
_TOKEN_PREFIX    = 'hd:token:'         # {token} → file metadata hash
_TOKEN_TTL       = 120                 # seconds user has to start the download

_FORMAT_ID_RE    = re.compile(r'^[\w.-]{1,20}$')
_TOKEN_RE        = re.compile(r'^[a-f0-9]{32}$')

# ---------------------------------------------------------------------------
# Redis client
# ---------------------------------------------------------------------------
redis_client = redis_lib.Redis(
    host=os.environ.get('REDIS_HOST', 'redis'),
    port=int(os.environ.get('REDIS_PORT', 6379)),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=5,
)

# ---------------------------------------------------------------------------
# Lua script — atomic slot acquisition with automatic stale-slot eviction.
#
# Stale slots (older than DOWNLOAD_TIMEOUT + 2-min buffer) are removed first,
# so a crashed worker can never permanently block a slot.
# Returns 1 on success, 0 if at capacity.
# ---------------------------------------------------------------------------
_ACQUIRE_SLOT_LUA = """
local key          = KEYS[1]
local now          = tonumber(ARGV[1])
local max_slots    = tonumber(ARGV[2])
local slot_id      = ARGV[3]
local stale_cutoff = now - tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, '-inf', stale_cutoff)
if redis.call('ZCARD', key) >= max_slots then return 0 end
redis.call('ZADD', key, now, slot_id)
return 1
"""
_acquire_slot_script = redis_client.register_script(_ACQUIRE_SLOT_LUA)

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _acquire_slot(slot_id: str) -> bool:
    result = _acquire_slot_script(
        keys=[_SLOT_KEY],
        args=[time.time(), MAX_CONCURRENT_DOWNLOADS, slot_id,
              DOWNLOAD_TIMEOUT + 120],   # stale after 12 minutes
    )
    return result == 1


def _release_slot(slot_id: str) -> None:
    try:
        redis_client.zrem(_SLOT_KEY, slot_id)
    except redis_lib.RedisError:
        pass  # best-effort; stale eviction in the Lua script is the safety net


def _hd_rate_limited(ip: str) -> bool:
    """True → request should be rejected. Redis INCR is atomic; safe to use without a lock."""
    key = f'{_HD_RATE_PREFIX}{ip}'
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, HD_RATE_WINDOW)
        return count > HD_RATE_LIMIT
    except redis_lib.RedisError:
        return False   # fail open: Redis down → don't block users


def _store_token(token: str, path: str, name: str, size: int,
                 slot_id: str, tmp_dir: str) -> None:
    redis_client.hset(f'{_TOKEN_PREFIX}{token}', mapping={
        'path': path, 'name': name,
        'size': str(size), 'slot_id': slot_id, 'tmp_dir': tmp_dir,
    })
    redis_client.expire(f'{_TOKEN_PREFIX}{token}', _TOKEN_TTL)


def _consume_token(token: str) -> dict | None:
    """Single-use: reads and immediately deletes the token."""
    key = f'{_TOKEN_PREFIX}{token}'
    data = redis_client.hgetall(key)
    if not data:
        return None
    redis_client.delete(key)
    return data

# ---------------------------------------------------------------------------
# General (in-memory) rate limiter — used by the fast /extract/ endpoint
# ---------------------------------------------------------------------------

def _get_client_ip(request) -> str:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')


def _is_rate_limited(ip: str) -> bool:
    now    = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    history = [t for t in _rate_store.get(ip, []) if t > cutoff]
    if len(history) >= RATE_LIMIT_REQUESTS:
        return True
    _rate_store[ip] = history + [now]
    return False


def _validate_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ('http', 'https') and p.netloc.lower() in ALLOWED_DOMAINS
    except Exception:
        return False


def _validate_format_id(fid: str) -> bool:
    return bool(fid and _FORMAT_ID_RE.match(fid))

# ---------------------------------------------------------------------------
# Format extraction
# ---------------------------------------------------------------------------
DIRECT_TIERS = [(720, '720p'), (480, '480p'), (360, '360p')]
SERVER_TIERS = [(2160, '4K'), (1440, '1440p'), (1080, '1080p')]


def extract_formats(formats: list, duration: float) -> list:
    streams: list = []
    seen_heights: set = set()

    # Direct (progressive: audio + video in one file)
    progressive = [
        f for f in formats
        if f.get('vcodec', 'none') != 'none'
        and f.get('acodec', 'none') != 'none'
        and f.get('url')
    ]
    for max_h, label in DIRECT_TIERS:
        candidates = [f for f in progressive if f.get('height') and f['height'] <= max_h]
        if not candidates:
            continue
        best = max(candidates, key=lambda f: f.get('tbr') or f.get('vbr') or 0)
        h = best['height']
        if h in seen_heights:
            continue
        seen_heights.add(h)
        streams.append({
            'quality':   label, 'height': h, 'ext': best.get('ext', 'mp4'),
            'url':       best['url'],
            'filesize':  best.get('filesize') or best.get('filesize_approx'),
            'type':      'direct', 'format_id': None,
        })

    # Server-required (video-only 1080p+, needs ffmpeg merge)
    if duration and duration <= MAX_DURATION_SECONDS:
        video_only = [
            f for f in formats
            if f.get('vcodec', 'none') != 'none'
            and f.get('acodec', 'none') == 'none'
            and f.get('url')
            and (f.get('height') or 0) >= 1080
            and f.get('format_id')
        ]
        for max_h, label in SERVER_TIERS:
            candidates = [f for f in video_only if f.get('height') and f['height'] <= max_h]
            if not candidates:
                continue
            best = max(candidates, key=lambda f: f.get('tbr') or f.get('vbr') or 0)
            h = best['height']
            if h in seen_heights:
                continue
            size = best.get('filesize') or best.get('filesize_approx')
            if size and size > MAX_FILESIZE_BYTES:
                continue
            seen_heights.add(h)
            streams.append({
                'quality':   label, 'height': h, 'ext': 'mp4',
                'url':       None,
                'filesize':  size,
                'type':      'server_required', 'format_id': best['format_id'],
            })

    streams.sort(key=lambda s: s['height'] or 0, reverse=True)
    return streams

# ---------------------------------------------------------------------------
# Streaming generator
# Releases the Redis slot and deletes the temp dir after the last byte is
# sent (or on any error/client disconnect). This is the ONLY place the slot
# is released for successful downloads, ensuring it covers the full lifecycle.
# ---------------------------------------------------------------------------

def _stream_release_delete(path: str, tmp_dir: str, slot_id: str,
                            chunk_size: int = 65_536):
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(chunk_size):
                yield chunk
    finally:
        _release_slot(slot_id)
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# View 1: POST /api/extract/
# (unchanged from previous implementation)
# ---------------------------------------------------------------------------

@api_view(['POST'])
def extract(request):
    ip = _get_client_ip(request)
    if _is_rate_limited(ip):
        return Response(
            {'error': 'Too many requests. Please wait a moment and try again.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    url = request.data.get('url', '').strip()
    if not url:
        return Response({'error': "Missing 'url'."}, status=status.HTTP_400_BAD_REQUEST)
    if not _validate_url(url):
        return Response(
            {'error': 'Only YouTube URLs are allowed (youtube.com, youtu.be).'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        result = subprocess.run(
            ['yt-dlp', '-J', '--no-playlist', url],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return Response({'error': 'Metadata fetch timed out. Try again.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
    except FileNotFoundError:
        return Response({'error': 'yt-dlp is not installed on this server.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if result.returncode != 0:
        return Response(
            {'error': 'Could not fetch video info. The video may be unavailable, private, or age-restricted.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Response({'error': 'Failed to parse video metadata.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    duration = info.get('duration') or 0
    streams  = extract_formats(info.get('formats', []), duration)

    return Response({
        'title':     info.get('title', 'Unknown title'),
        'thumbnail': info.get('thumbnail', ''),
        'duration':  duration,
        'streams':   streams,
    })

# ---------------------------------------------------------------------------
# View 2: POST /api/download-high-quality/
#
# Runs yt-dlp + ffmpeg, then returns a short-lived single-use token.
# The slot is acquired here and HELD — it is only released by the streaming
# generator in View 3 after the last byte is delivered to the client.
# ---------------------------------------------------------------------------

@api_view(['POST'])
def download_high_quality(request):
    ip = _get_client_ip(request)

    # Strict per-IP rate limit for the heavy HD endpoint
    if _hd_rate_limited(ip):
        return Response(
            {'error': 'HD download limit reached (max 3 per 10 minutes). Please try again later.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    url       = request.data.get('url', '').strip()
    format_id = request.data.get('format_id', '').strip()

    if not _validate_url(url):
        return Response({'error': 'Only YouTube URLs are allowed.'}, status=status.HTTP_400_BAD_REQUEST)
    if not _validate_format_id(format_id):
        return Response({'error': 'Invalid format ID.'}, status=status.HTTP_400_BAD_REQUEST)

    # ── Acquire global slot (held until streaming completes in View 3) ────
    slot_id = uuid.uuid4().hex
    try:
        acquired = _acquire_slot(slot_id)
    except redis_lib.RedisError:
        return Response({'error': 'Service temporarily unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if not acquired:
        return Response(
            {'error': 'Server is busy (max 2 concurrent HD downloads). Please try again shortly.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    tmp_dir  = tempfile.mkdtemp()
    uid      = uuid.uuid4().hex
    out_tmpl = os.path.join(tmp_dir, f'{uid}_%(title)s.%(ext)s')
    success  = False   # gates the finally cleanup

    try:
        proc = subprocess.run(
            [
                'yt-dlp',
                '-f', f'{format_id}+bestaudio/best',
                '--merge-output-format', 'mp4',
                '--concurrent-fragments', '5',
                '-o', out_tmpl,
                url,
            ],
            capture_output=True,
            timeout=DOWNLOAD_TIMEOUT,
        )

        if proc.returncode != 0:
            return Response(
                {'error': 'Download failed. The video may be unavailable or the format unsupported.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        matches = [
            f for f in os.listdir(tmp_dir)
            if f.startswith(uid + '_') and f.endswith('.mp4')
        ]
        if not matches:
            return Response(
                {'error': 'Processed file not found after download.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        out_path     = os.path.join(tmp_dir, matches[0])
        display_name = matches[0][len(uid) + 1:]
        file_size    = os.path.getsize(out_path)

        if file_size > MAX_FILESIZE_BYTES:
            return Response(
                {'error': 'File exceeds the 1 GB size limit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Store file metadata under a single-use token (TTL = 2 minutes).
        # The slot is NOT released here — View 3 releases it after streaming.
        token = uuid.uuid4().hex
        _store_token(token, out_path, display_name, file_size, slot_id, tmp_dir)
        success = True
        return Response({'token': token})

    except subprocess.TimeoutExpired:
        return Response(
            {'error': 'Download timed out (10-minute limit). Try a shorter video or lower quality.'},
            status=status.HTTP_504_GATEWAY_TIMEOUT,
        )
    except Exception:
        return Response({'error': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    finally:
        # On any error path (success=False): release slot + clean up temp dir.
        # On success (success=True): do nothing — View 3 owns the slot and temp dir.
        if not success:
            _release_slot(slot_id)
            shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# View 3: GET /api/serve-download/<token>/
#
# Single-use, short-lived. Triggered by the browser navigating to this URL
# (via an <a href> click in the frontend). No JS memory buffering — the
# browser streams directly from this HTTP response to disk.
# Releases the concurrency slot after the last byte is sent.
# ---------------------------------------------------------------------------

@api_view(['GET'])
def serve_download(request, token):
    if not _TOKEN_RE.match(token):
        return Response({'error': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        info = _consume_token(token)
    except redis_lib.RedisError:
        return Response({'error': 'Service temporarily unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if not info:
        return Response(
            {'error': 'Download link not found or already used. Please go back and try again.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    path         = info['path']
    display_name = info['name']
    file_size    = int(info['size'])
    slot_id      = info['slot_id']
    tmp_dir      = info['tmp_dir']

    if not os.path.exists(path):
        _release_slot(slot_id)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return Response({'error': 'File not found on server.'}, status=status.HTTP_404_NOT_FOUND)

    encoded = urllib.parse.quote(display_name, safe='')
    resp = StreamingHttpResponse(
        _stream_release_delete(path, tmp_dir, slot_id),
        content_type='video/mp4',
    )
    resp['Content-Length']      = str(file_size)
    resp['Content-Disposition'] = (
        f'attachment; filename="{display_name}"; filename*=UTF-8\'\'{encoded}'
    )
    return resp
