import wavelink
import discord
from discord.ext import commands, tasks
import logging
import os
import asyncio
import json
import collections
import time
import urllib.parse
import urllib.request

import aiohttp
import re
import base64

from utils import db

logger = logging.getLogger("nexus.music")


# ── Spotify API credentials (from .env) ───────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    logger.warning(
        "⚠️ SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set in .env — "
        "Spotify link support will be disabled until these are configured."
    )
_spotify_token: str | None = None
_spotify_token_expiry: float = 0.0

PRIMARY_NODE_ID = "Primary-Node"

_spotify_token_lock = asyncio.Lock()

async def _get_spotify_token(session: aiohttp.ClientSession) -> str | None:
    """Get a fresh Spotify access token using client_credentials flow."""
    global _spotify_token, _spotify_token_expiry

    async with _spotify_token_lock:
        if _spotify_token and time.time() < _spotify_token_expiry - 30:
            return _spotify_token

        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            logger.error("❌ Spotify credentials not set in .env!")
            return None

        credentials = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()

        try:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data="grant_type=client_credentials",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _spotify_token = data.get("access_token")
                    _spotify_token_expiry = time.time() + data.get("expires_in", 3600)
                    logger.info("✅ Spotify API token refreshed successfully")
                    return _spotify_token
                else:
                    logger.error(f"❌ Spotify token fetch failed: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"❌ Spotify token fetch error: {e}")
        return None
_spotify_cache = collections.OrderedDict()
_SPOTIFY_CACHE_SIZE = 500

# ── YouTube Music "Liked Music" support (via ytmusicapi) ──────────────────
# YouTube's public playlist-browsing API caps the special auto-generated
# "Liked Music" list (list=LM) at roughly 100-120 items for every tool that
# reads it through the public endpoint (Lavalink included) — this is a
# YouTube-side limitation, not something fixable via Lavalink config.
# ytmusicapi authenticates as the logged-in user and can fetch the full
# liked-songs list instead.
_ytmusic_client = None
_ytmusic_client_error_logged = False

def _get_ytmusic_client():
    """Lazily create and cache a YTMusic client from the configured auth
    file. Returns None (and logs once) if ytmusicapi isn't installed or no
    valid auth file is found."""
    global _ytmusic_client, _ytmusic_client_error_logged
    if _ytmusic_client is not None:
        return _ytmusic_client

    try:
        from ytmusicapi import YTMusic
    except ImportError:
        if not _ytmusic_client_error_logged:
            logger.warning("⚠️ ytmusicapi is not installed — 'Liked Music' playlist links will not work.")
            _ytmusic_client_error_logged = True
        return None

    auth_file = os.getenv("YTMUSIC_AUTH_FILE", "oauth.json")
    if not os.path.exists(auth_file):
        if not _ytmusic_client_error_logged:
            logger.warning(
                f"⚠️ YouTube Music auth file '{auth_file}' not found — 'Liked Music' "
                f"playlist links will not work until you generate it (run "
                f"`ytmusicapi oauth` in the bot's working directory, or set "
                f"YTMUSIC_AUTH_FILE in .env to point at an existing auth file)."
            )
            _ytmusic_client_error_logged = True
        return None

    try:
        _ytmusic_client = YTMusic(auth_file)
        logger.info(f"✅ ytmusicapi initialised using auth file '{auth_file}'.")
        return _ytmusic_client
    except Exception as e:
        if not _ytmusic_client_error_logged:
            logger.error(f"⚠️ Failed to initialise ytmusicapi with '{auth_file}': {e}")
            _ytmusic_client_error_logged = True
        return None


async def resolve_liked_music_queries(limit: int = 3000) -> list[str]:
    """Fetch the authenticated user's YouTube Music 'Liked Music' list via
    ytmusicapi and return playable watch-URL queries for wavelink. Runs the
    (synchronous) ytmusicapi call in a thread so it doesn't block the event
    loop. Returns an empty list if ytmusicapi isn't configured — callers
    should treat that as "not available" and tell the user why."""
    client = _get_ytmusic_client()
    if client is None:
        return []

    def _fetch():
        data = client.get_liked_songs(limit=limit)
        return data.get("tracks", []) if data else []

    try:
        tracks = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.error(f"❌ ytmusicapi get_liked_songs failed: {e}")
        return []

    queries = []
    for t in tracks:
        video_id = t.get("videoId")
        if video_id:
            queries.append(f"https://www.youtube.com/watch?v={video_id}")
    return queries

async def resolve_youtube_playlist_queries(playlist_id: str, limit: int | None = None) -> list[str]:
    """
    Fetch a REGULAR (non-Liked-Music) YouTube/YouTube Music playlist's full
    track list via ytmusicapi, so it can use the same instant-first-track +
    background-load pattern as Spotify and Liked Music, instead of Lavalink's
    native playlist load — which resolves the ENTIRE playlist in one blocking
    call before returning anything (hence the "this may take a minute or two"
    wait on large playlists). Returns [] if ytmusicapi isn't configured or the
    fetch fails — callers should fall back to the native Lavalink load in
    that case.
    """
    client = _get_ytmusic_client()
    if client is None:
        return []

    def _fetch():
        data = client.get_playlist(playlist_id, limit=limit)
        return data.get("tracks", []) if data else []

    try:
        tracks = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"ytmusicapi get_playlist failed for {playlist_id}, will fall back to native load: {e}")
        return []

    queries = []
    for t in tracks:
        video_id = t.get("videoId")
        if video_id:
            queries.append(f"https://www.youtube.com/watch?v={video_id}")
    return queries

async def _fetch_spotify_playlist_tracks_full(item_type: str, item_id: str, session: aiohttp.ClientSession) -> list[dict]:
    """
    Fetch ALL tracks of a Spotify playlist or album via the official Web API
    (Client Credentials flow — no user login needed), paginating past the
    ~100-item cap that the embed-page scrape is stuck with.
    Returns a list of dicts: {"query": "ytmsearch:artist title", "duration_ms": 213000, "artist": "Artist Name"},
    or [] on failure (caller should fall back to the embed scrape in that case).
    """
    token = await _get_spotify_token(session)
    if not token:
        return []

    endpoint = (
        f"https://api.spotify.com/v1/playlists/{item_id}/tracks"
        if item_type == "playlist"
        else f"https://api.spotify.com/v1/albums/{item_id}/tracks"
    )
    headers = {"Authorization": f"Bearer {token}"}
    queries: list[dict] = []
    offset = 0
    limit = 100

    try:
        while True:
            params = {"limit": str(limit), "offset": str(offset)}
            async with session.get(endpoint, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ Spotify API playlist fetch returned HTTP {resp.status} at offset {offset}")
                    break
                data = await resp.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                # Playlist items wrap the track under "track"; album items ARE the track.
                track = item.get("track", item) if item_type == "playlist" else item
                if not track:
                    continue
                title = track.get("name", "")
                artists = track.get("artists", [])
                artist_name = artists[0]["name"] if artists else ""
                duration_ms = track.get("duration_ms")
                if title:
                    clean_q = f"{artist_name} {title}".replace('&', '').replace('  ', ' ').strip()
                    queries.append({
                        "query": f"ytmsearch:{clean_q}",
                        "duration_ms": duration_ms,
                        "artist": artist_name
                    })

            if data.get("next") is None:
                break
            offset += limit
            # Be gentle on Spotify's API between pages.
            await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"❌ Spotify API pagination error for {item_type}/{item_id} at offset {offset}: {e}")

    return queries


async def resolve_spotify_query(query: str, session: aiohttp.ClientSession = None) -> list[dict]:
    """
    Convert Spotify URLs into YouTube search queries by scraping the Spotify Embed page.
    This bypasses the need for the official API (which is now paywalled for free tiers).
    Returns list of dicts: {"query": "ytmsearch:...", "duration_ms": int | None, "artist": str | None}
    """
    if "open.spotify.com" not in query and "spotify.com" not in query:
        return [{"query": query, "duration_ms": None, "artist": None}]

    if query in _spotify_cache:
        logger.info("⚡ Using high-speed memory cache for Spotify query")
        _spotify_cache.move_to_end(query)
        return _spotify_cache[query]

    # Extract item type and ID
    match = re.search(r'spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)', query)
    if not match:
        logger.warning(f"Could not parse Spotify URL: {query}")
        return [{"query": f"ytmsearch:{query}", "duration_ms": None, "artist": None}]

    item_type, item_id = match.groups()
    queries: list[dict] = []

    # For playlists/albums, try the official API first — it paginates past
    # the ~100-item cap the embed-page scrape below is stuck with. Falls
    # through to the embed scrape only if the API call fails (e.g. Spotify
    # credentials not configured).
    if item_type in ["playlist", "album"]:
        _session_for_api = session or aiohttp.ClientSession()
        try:
            queries = await _fetch_spotify_playlist_tracks_full(item_type, item_id, _session_for_api)
        finally:
            if session is None:
                await _session_for_api.close()

        if queries:
            if len(_spotify_cache) >= _SPOTIFY_CACHE_SIZE:
                _spotify_cache.popitem(last=False)
            _spotify_cache[query] = queries
            return queries
        logger.warning(f"Spotify API pagination returned no tracks for {item_type}/{item_id} — falling back to embed scrape (capped at ~100 tracks).")

    try:
        _temp_session = None
        if session is None:
            _temp_session = aiohttp.ClientSession()
            session = _temp_session

        embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
        
        async with session.get(embed_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"⚠️ Spotify Embed page returned HTTP {resp.status}")
                if _temp_session: await _temp_session.close()
                return [{"query": f"ytmsearch:spotify {item_type} {item_id}", "duration_ms": None, "artist": None}]
            
            html = await resp.text()
            json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
            if not json_match:
                if _temp_session: await _temp_session.close()
                return [{"query": f"ytmsearch:spotify {item_type} {item_id}", "duration_ms": None, "artist": None}]
                
            data = json.loads(json_match.group(1))
            entity = data['props']['pageProps']['state']['data']['entity']
                
            if item_type == "track":
                title = entity.get('title', '')
                subtitle = entity.get('subtitle', '') or ''
                artist_name = subtitle.split(',')[0].strip() if subtitle else ''
                artists = entity.get('artists', [])
                if artists and isinstance(artists, list) and isinstance(artists[0], dict):
                    artist_name = artists[0].get('name', artist_name)
                duration_ms = entity.get('duration') or entity.get('duration_ms')
                if title:
                    clean_q = f"{artist_name} {title}".replace('&', '').replace('  ', ' ').strip()
                    queries.append({
                        "query": f"ytmsearch:{clean_q}",
                        "duration_ms": duration_ms,
                        "artist": artist_name
                    })
                
            elif item_type in ["playlist", "album"]:
                track_list = entity.get('trackList', [])
                for item in track_list:
                    title = item.get('title', '')
                    subtitle = item.get('subtitle', '') or ''
                    artist_name = subtitle.split(',')[0].strip() if subtitle else ''
                    duration_ms = item.get('duration') or item.get('duration_ms')
                    if title:
                        clean_q = f"{artist_name} {title}".replace('&', '').replace('  ', ' ').strip()
                        queries.append({
                            "query": f"ytmsearch:{clean_q}",
                            "duration_ms": duration_ms,
                            "artist": artist_name
                        })
            
            elif item_type == "artist":
                title = entity.get('name', entity.get('title', ''))
                if title:
                    queries.append({
                        "query": f"ytmsearch:{title} top tracks".strip(),
                        "duration_ms": None,
                        "artist": title
                    })

        if queries:
            if len(_spotify_cache) >= _SPOTIFY_CACHE_SIZE:
                _spotify_cache.popitem(last=False)
            _spotify_cache[query] = queries
            
    except Exception as e:
        logger.error(f"❌ Spotify Scrape Error: {e}")
    finally:
        if _temp_session:
            await _temp_session.close()

    # Fallback if nothing was extracted
    if not queries:
        queries.append({
            "query": f"ytmsearch:spotify {item_type} {item_id}",
            "duration_ms": None,
            "artist": None
        })

    return queries



async def search_spotify_api(query: str, session: aiohttp.ClientSession) -> tuple[str | None, str | None]:
    """
    Search the official Spotify API for a text query.
    Returns (clean_search_query, spotify_track_uri) or (None, None).
    """
    token = await _get_spotify_token(session)
    if not token:
        return None, None

    try:
        async with session.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "type": "track", "limit": "1"},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                tracks = data.get("tracks", {}).get("items", [])
                if tracks:
                    track = tracks[0]
                    artist_name = track["artists"][0]["name"]
                    track_name = track["name"]
                    uri = track["uri"]
                    return f"{artist_name} {track_name}", uri
    except Exception as e:
        logger.error(f"Spotify API search failed: {e}")
    return None, None


async def get_spotify_recommendation(seed_tracks: list[str], session: aiohttp.ClientSession) -> str | None:
    """
    Get a recommended track from Spotify based on seed track URIs/IDs.
    Returns the artist and track name to search for on YouTube.
    """
    token = await _get_spotify_token(session)
    if not token or not seed_tracks:
        return None

    # Clean URIs to just IDs
    seed_ids = [uri.replace("spotify:track:", "") for uri in seed_tracks if "track" in uri]
    if not seed_ids:
        return None
    
    # Spotify allows max 5 seeds
    seed_ids = seed_ids[:5]

    try:
        async with session.get(
            "https://api.spotify.com/v1/recommendations",
            headers={"Authorization": f"Bearer {token}"},
            params={"seed_tracks": ",".join(seed_ids), "limit": "1"},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                tracks = data.get("tracks", [])
                if tracks:
                    track = tracks[0]
                    artist_name = track["artists"][0]["name"]
                    track_name = track["name"]
                    track_uri = track.get("uri") or f"spotify:track:{track.get('id')}"
                    return f"{artist_name} {track_name}", track_uri
    except Exception as e:
        logger.error(f"Spotify API recommendations failed: {e}")
    return None



def rank_best_track(tracks: list[wavelink.Playable], query: str) -> wavelink.Playable:
    """
    Next-Level Smart Search Ranking Algorithm.
    Evaluates YouTube search results to pick official original tracks over short covers/shorts.
    """
    if not tracks:
        return None
    if not isinstance(tracks, (list, tuple)) and hasattr(tracks, 'tracks'):
        tracks_list = list(tracks.tracks)
    elif isinstance(tracks, (list, tuple)):
        tracks_list = list(tracks)
    elif hasattr(tracks, 'title'):
        return tracks
    else:
        return None

    if not tracks_list:
        return None
    if len(tracks_list) == 1:
        return tracks_list[0]

    best_track = tracks_list[0]
    best_score = -9999
    q_lower = query.lower().replace("ytsearch:", "").replace("ytmsearch:", "")

    for track in tracks_list:
        score = 0
        dur_ms = getattr(track, "length", 0) or 0
        dur_sec = dur_ms / 1000
        author_lower = (getattr(track, "author", "") or "").lower()
        title_lower = (getattr(track, "title", "") or "").lower()

        # 1. Penalize short videos/shorts (<60 seconds)
        if dur_sec < 60:
            score -= 300
        elif 120 <= dur_sec <= 420: # Ideal song length 2 to 7 mins
            score += 50

        # 2. Reward Official / Topic / VEVO channels
        if "official" in author_lower or "vevo" in author_lower or "- topic" in author_lower or "sanuka" in author_lower:
            score += 40
        if "official" in title_lower or "audio" in title_lower or "music video" in title_lower:
            score += 30

        # 3. Reward artist / query word match
        for word in q_lower.split():
            if len(word) > 2:
                if word in author_lower:
                    score += 25
                if word in title_lower:
                    score += 15

        # 4. Penalize covers/remixes/tik tok versions unless specifically requested
        if "cover" in title_lower and "cover" not in q_lower:
            score -= 50
        if "tiktok" in title_lower and "tiktok" not in q_lower:
            score -= 30
        if "ringtone" in title_lower and "ringtone" not in q_lower:
            score -= 100

        # 5. Massive Bonus for Real 24/7 Live Streams & Official Lofi Girl channel
        if getattr(track, "is_stream", False) or "lofi girl" in author_lower or "chilledcow" in author_lower:
            score += 500

        if score > best_score:
            best_score = score
            best_track = track


    return best_track


def pick_best_spotify_match(
    tracks: list[wavelink.Playable],
    target_duration_ms: int | None,
    target_artist: str | None,
    duration_tolerance_ms: int = 3000,
) -> wavelink.Playable | None:
    """
    Layered exact-match picker for Spotify-originated searches:
    1. Filter to candidates within duration_tolerance_ms of the
       Spotify track's duration (if target_duration_ms is known).
    2. Among those, STRICTLY require the YouTube channel/author name
       to contain at least one significant word (len > 2) from
       target_artist (if target_artist is known) — this is a hard
       filter, not a scoring bonus.
    3. If candidates remain after both filters, pass them to the
       existing rank_best_track() heuristic to pick the best of
       those (official channel, "official audio" in title, etc.)
       as the final tiebreaker.
    4. If NO candidates survive step 1+2 (zero matches within
       duration+artist), fall back to rank_best_track() on the full
       unfiltered candidate list — better to return a loosely-matched
       result than nothing, but this is the last resort, not the
       default path.
    """
    if not tracks:
        return None
    if not isinstance(tracks, (list, tuple)) and hasattr(tracks, 'tracks'):
        tracks_list = list(tracks.tracks)
    elif isinstance(tracks, (list, tuple)):
        tracks_list = list(tracks)
    elif hasattr(tracks, 'title'):
        return tracks
    else:
        return None

    if not tracks_list:
        return None

    candidates = tracks_list
    if target_duration_ms:
        duration_filtered = [
            t for t in tracks_list
            if getattr(t, "length", None) is not None and abs(getattr(t, "length", 0) - target_duration_ms) <= duration_tolerance_ms
        ]
        if duration_filtered:
            candidates = duration_filtered

    if target_artist:
        artist_words = [w.lower() for w in target_artist.split() if len(w) > 2]
        if artist_words:
            artist_filtered = [
                t for t in candidates
                if any(w in (getattr(t, "author", "") or "").lower() for w in artist_words)
            ]
            if artist_filtered:
                candidates = artist_filtered

    # candidates is now the best-available filtered set (could still
    # be the full unfiltered list if both filters found zero matches)
    return rank_best_track(candidates, target_artist or "")


async def _native_youtube_search(query: str) -> str | None:
    """Bypass Lavalink IP bans by scraping YouTube natively from the Python host."""
    try:
        def scrape():
            search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
            req = urllib.request.Request(search_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
            html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
            video_ids = re.findall(r"watch\?v=(\S{11})", html)
            if video_ids:
                return f"https://www.youtube.com/watch?v={video_ids[0]}"
            logger.warning(f"Native YT search returned 0 matches for query: {query}")
            return None
        return await asyncio.get_event_loop().run_in_executor(None, scrape)
    except Exception as e:
        logger.error(f"Native YT search failed: {e}")
        return None

async def _native_youtube_playlist_scrape(playlist_id: str) -> list[dict]:
    """Scrape playlist tracks natively from Python if Lavalink fails to resolve the playlist."""
    try:
        def scrape():
            url = f"https://www.youtube.com/playlist?list={playlist_id}"
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'}
            )
            html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore')
            tracks_data = []
            seen = set()

            # Strategy 1: Parse ytInitialData JSON for rich title, author, and videoId
            has_error_alert = False
            idx = html.find('ytInitialData = {')
            if idx != -1:
                start = idx + len('ytInitialData = ')
                end = html.find(';</script>', start)
                if end != -1:
                    try:
                        data = json.loads(html[start:end])
                        if "alerts" in data:
                            for a in data.get("alerts", []):
                                if a.get("alertRenderer", {}).get("type") == "ERROR":
                                    has_error_alert = True
                                    logger.warning(f"YouTube playlist {playlist_id} is private or does not exist.")
                                    return []

                        tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
                        for t in tabs:
                            pl_items = (
                                t.get("tabRenderer", {})
                                .get("content", {})
                                .get("sectionListRenderer", {})
                                .get("contents", [{}])[0]
                                .get("itemSectionRenderer", {})
                                .get("contents", [{}])[0]
                                .get("playlistVideoListRenderer", {})
                                .get("contents", [])
                            )
                            for it in pl_items:
                                vr = it.get("playlistVideoRenderer")
                                if vr and "videoId" in vr:
                                    vid = vr["videoId"]
                                    if vid not in seen:
                                        seen.add(vid)
                                        title = vr.get("title", {}).get("runs", [{}])[0].get("text", "")
                                        author = vr.get("shortBylineText", {}).get("runs", [{}])[0].get("text", "")
                                        tracks_data.append({
                                            "id": vid,
                                            "url": f"https://www.youtube.com/watch?v={vid}",
                                            "title": title,
                                            "author": author
                                        })
                    except Exception as json_ex:
                        logger.debug(f"ytInitialData JSON parse error: {json_ex}")

            # Strategy 2: Only fallback to regex if JSON extraction was clean and not an error page
            if not tracks_data and not has_error_alert and "The playlist does not exist" not in html and "alertRenderer" not in html:
                video_ids = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", html)
                for vid in video_ids:
                    if vid not in seen:
                        seen.add(vid)
                        tracks_data.append({
                            "id": vid,
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "title": "",
                            "author": ""
                        })
            return tracks_data
        return await asyncio.get_event_loop().run_in_executor(None, scrape)
    except Exception as e:
        logger.error(f"Native playlist scrape failed for {playlist_id}: {e}")
        return []

async def _resolve_playable_track(track_info: dict | str) -> wavelink.Playable | None:
    """Safely resolve a track via direct YouTube URL, or YouTube Music / YouTube search."""
    url = track_info["url"] if isinstance(track_info, dict) else str(track_info)
    title = track_info.get("title", "") if isinstance(track_info, dict) else ""
    author = track_info.get("author", "") if isinstance(track_info, dict) else ""

    # Try 1: Direct YouTube URL on Lavalink
    try:
        res = await asyncio.wait_for(wavelink.Playable.search(url), timeout=12.0)
        if res:
            t = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
            if title:
                setattr(t, "_title_override", title)
            return t
    except Exception:
        pass

    # Try 2: YouTube Music search by title/author
    if title:
        search_term = f"{author} {title}".strip()
        try:
            res = await asyncio.wait_for(wavelink.Playable.search(search_term, source=wavelink.TrackSource.YouTubeMusic), timeout=12.0)
            if res:
                t = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                setattr(t, "_title_override", title)
                return t
        except Exception:
            pass

    # Try 3: Standard YouTube search
    if title:
        try:
            res = await asyncio.wait_for(wavelink.Playable.search(f"ytsearch:{author} {title}".strip()), timeout=12.0)
            if res:
                t = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                setattr(t, "_title_override", title)
                return t
        except Exception:
            pass

    return None

def _player_valid(player: wavelink.Player | None) -> bool:
    """Return True only if the player object exists and is still connected."""
    if player is None:
        return False
    return getattr(player, "channel", None) is not None

async def _ensure_node_connected(bot):
    """Ensure at least one node is connected, attempting instant reconnect to private node if needed."""
    if not wavelink.Pool.nodes or all(n.status != wavelink.NodeStatus.CONNECTED for n in wavelink.Pool.nodes.values()):
        host = os.getenv("LAVALINK_HOST")
        port = os.getenv("LAVALINK_PORT")
        pw = os.getenv("LAVALINK_PASSWORD")
        if host and port:
            try:
                uri = f"http://{host}:{port}"
                node = wavelink.Node(uri=uri, password=pw, identifier=PRIMARY_NODE_ID)
                await asyncio.wait_for(wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100), timeout=5.0)
            except Exception:
                pass


class VoiceChannelPermissionError(discord.ClientException):
    """Raised when the bot lacks Connect or Speak permission in a voice channel.

    Subclasses discord.ClientException so broad ``except Exception`` callers still
    catch it, but it can be caught specifically to show a permission-specific message
    rather than the generic "voice connection timeout or error" fallback.

    Attributes
    ----------
    channel: discord.VoiceChannel
        The channel the bot tried to join.
    missing: list[str]
        Names of the missing permissions, e.g. ``['connect', 'speak']``.
    """

    def __init__(self, channel: discord.VoiceChannel, missing: list[str]) -> None:
        self.channel = channel
        self.missing = missing
        perms = " and ".join(f"**{p}**" for p in missing)
        super().__init__(
            f"I don't have {perms} permission in {channel.mention}. "
            f"Please grant me the required permissions and try again."
        )


async def _connect_voice_with_retry(
    channel: discord.VoiceChannel,
    retries: int = 2,
    timeout: float = 15.0,
    bot: discord.Client | None = None,
) -> wavelink.Player:
    """
    Connect to a voice channel with retry-with-backoff and stale client cleanup.

    Retry policy (transient failures only):
    - asyncio.TimeoutError  — Discord voice gateway handshake timed out; worth retrying.
    - discord.ClientException("Already connected …") — stale ghost VoiceClient in the
      connection state; cleared between attempts and worth retrying.
    All other exceptions (e.g. opus.OpusNotLoaded) are non-transient and fail fast
    on attempt 1 without waiting.

    Before every attempt (including the first), any orphaned/ghost voice client in the
    guild is force-disconnected so Discord's state machine is clean before we try to
    connect.

    On total failure after `retries` attempts, detailed diagnostics are logged at ERROR
    level and the original exception is re-raised for the caller's existing handler.
    """
    # Transient errors that are worth retrying.
    _TRANSIENT = (
        asyncio.TimeoutError,
        discord.ClientException,    # "Already connected to a voice channel."
    )

    def _is_transient(exc: Exception) -> bool:
        # discord.ClientException covers both the "Already connected" stale-state
        # error and the generic connection error; non-transient subclasses of
        # ClientException (none exist in discord.py 2.x) would need to be added
        # to a fast-fail list here if they ever appear.
        return isinstance(exc, _TRANSIENT)

    async def _clear_stale_vc() -> None:
        """Force-disconnect any ghost voice client lingering in this guild."""
        stale = channel.guild.voice_client
        if stale:
            try:
                await stale.disconnect(force=True)
            except Exception:
                pass

    # ── Permission pre-check (instant, no network I/O) ────────────────────────
    # A permissions failure is indistinguishable from a network timeout inside
    # channel.connect() — Discord simply never sends VOICE_SERVER_UPDATE, so
    # we wait the full timeout before failing. Check permissions first so we
    # fail fast (no 15s wait, no retries) with a clear message.
    # Race note: permissions could change between this check and the actual
    # connect attempt — this is an acceptable edge case; in that scenario the
    # genuine TimeoutError will still be raised and retried normally.
    me = channel.guild.me
    if me is not None:
        perms = channel.permissions_for(me)
        missing = []
        if not perms.connect:
            missing.append("connect")
        if not perms.speak:
            missing.append("speak")
        if missing:
            raise VoiceChannelPermissionError(channel, missing)

    last_error: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        # Clear stale clients BEFORE every attempt, including the first.
        await _clear_stale_vc()

        try:
            player: wavelink.Player = await channel.connect(cls=wavelink.Player, timeout=timeout)
            if attempt > 1:
                logger.info(
                    f"Voice connection to '{channel.name}' ({channel.id}) in guild '{channel.guild.name}' "
                    f"succeeded on retry attempt {attempt}/{retries}."
                )
            return player
        except wavelink.exceptions.InvalidNodeException:
            # Lavalink is down/offline — re-raise immediately so caller displays server offline warning
            raise
        except Exception as e:
            if not _is_transient(e):
                # Non-transient error (e.g. opus.OpusNotLoaded) — fail immediately,
                # no delay or retry, just diagnostic log and re-raise.
                logger.error(
                    f"Voice connection to '{channel.name}' ({channel.id}) in guild '{channel.guild.name}' ({channel.guild.id}) "
                    f"failed with non-transient error (no retry): {type(e).__name__}: {e}"
                )
                raise

            last_error = e
            if attempt < retries:
                logger.warning(
                    f"Voice connection to '{channel.name}' ({channel.id}) failed on attempt {attempt}/{retries} "
                    f"({type(e).__name__}: {e}). Waiting 2s before retry..."
                )
                await asyncio.sleep(2)
            else:
                latency_ms = (
                    f"{bot.latency * 1000:.1f}ms"
                    if (bot and getattr(bot, "latency", None) is not None)
                    else "unknown (bot not provided)"
                )
                ghost_vc = channel.guild.voice_client
                ghost_state = (
                    f"Present ({type(ghost_vc).__name__}, channel={getattr(ghost_vc, 'channel', None)})"
                    if ghost_vc
                    else "None"
                )
                try:
                    node_status = (
                        {nid: str(n.status) for nid, n in wavelink.Pool.nodes.items()}
                        if wavelink.Pool.nodes
                        else "No nodes"
                    )
                except Exception:
                    node_status = "unknown"

                logger.error(
                    f"Voice connection to '{channel.name}' ({channel.id}) in guild '{channel.guild.name}' ({channel.guild.id}) "
                    f"failed after {retries} attempts. "
                    f"Gateway latency: {latency_ms} | VoiceClient state: {ghost_state} | "
                    f"Lavalink nodes: {node_status} | Error: {type(e).__name__}: {e}"
                )
                raise last_error




# (Wavelink native autoplay monkeypatch removed: player.autoplay is permanently disabled)



def sanitize_md(text: str | None) -> str:
    """Escape markdown special characters to prevent formatting breakage and injection."""
    if not text:
        return ""
    return discord.utils.escape_markdown(str(text))


async def _ensure_author_in_voice(ctx: commands.Context, custom_msg: str | None = None) -> discord.VoiceChannel | None:
    """Verify that the command invoker is in a voice channel.

    Returns the author's VoiceChannel if present, otherwise sends a consistent
    error message and returns None.
    """
    if not getattr(ctx.author, "voice", None) or not ctx.author.voice.channel:
        msg = custom_msg or "❌ You need to be in a voice channel first."
        await ctx.send(msg, delete_after=10)
        return None
    return ctx.author.voice.channel


def build_now_playing_embed(player: wavelink.Player | None, track: wavelink.Playable | None = None) -> discord.Embed:
    """Build the classic, clean Now Playing embed matching the original UI panel layout."""
    curr_track = track or (player.current if player else None)
    if not curr_track:
        embed = discord.Embed(
            title="🎵 Now Playing",
            description="*Nothing is currently playing.*\nUse `!play <song name / link>` to start playback!",
            color=0x0284c7
        )
        embed.set_footer(text="Nexus Music Engine • High-Performance Audio")
        return embed

    raw_author = getattr(curr_track, "author", None) or "Unknown Artist"
    clean_author = sanitize_md(raw_author)
    author_name = clean_author[:37] + "..." if len(clean_author) > 40 else clean_author

    raw_title = getattr(curr_track, '_title_override', None) or getattr(curr_track, 'title', None) or "Unknown Track"
    track_title = sanitize_md(raw_title)
    track_uri = getattr(curr_track, 'uri', "") or ""

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**[{track_title}]({track_uri})**\nby **`{author_name}`**",
        color=0x0284c7
    )
    if getattr(curr_track, "artwork", None):
        embed.set_thumbnail(url=curr_track.artwork)
    elif track_uri and "youtube.com/watch?v=" in track_uri:
        video_id = track_uri.split("watch?v=")[-1].split("&")[0]
        embed.set_thumbnail(url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg")
    elif track_uri and "youtu.be/" in track_uri:
        video_id = track_uri.split("youtu.be/")[-1].split("?")[0]
        embed.set_thumbnail(url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg")

    if track and player and getattr(player, "current", None) != track:
        pos_ms = 0
    else:
        pos_ms = getattr(player, "position", 0) if player else 0
    dur_ms = getattr(curr_track, "length", 0) or 0
    
    pos_sec = int(pos_ms / 1000)
    dur_sec = int(dur_ms / 1000)

    pos_str = f"{pos_sec // 60:02d}:{pos_sec % 60:02d}"
    dur_str = f"{dur_sec // 60:02d}:{dur_sec % 60:02d}" if dur_ms > 0 else "LIVE"

    if dur_ms > 0:
        pct = min(1.0, max(0.0, pos_ms / dur_ms))
        bar_len = 10
        filled = int(pct * bar_len)
        bar = "─" * filled + "🔘" + "─" * (bar_len - filled)
        progress_val = f"`{pos_str}` [{bar}]\n`{dur_str}`"
    else:
        progress_val = "`00:00` [🔘──────────]\n`LIVE`"

    embed.add_field(name="⏱️ Progress", value=progress_val, inline=False)

    # ⚙️ Playback field
    vol = getattr(player, "volume", 100) if player else 100
    loop_str = "OFF"
    if player and hasattr(player, "queue"):
        if player.queue.mode == wavelink.QueueMode.loop:
            loop_str = "🔂 TRACK"
        elif player.queue.mode == wavelink.QueueMode.loop_all:
            loop_str = "🔁 QUEUE"
    embed.add_field(name="⚙️ Playback", value=f"**{vol}% • {loop_str}**", inline=True)

    # 📀 Track field
    music_cog = player.client.get_cog("music") if (player and getattr(player, "client", None)) else None
    guild_id = player.guild.id if (player and getattr(player, "guild", None)) else 0
    pos_num = music_cog._track_position.get(guild_id) if music_cog else None
    tot_num = music_cog._track_total.get(guild_id) if music_cog else None

    if pos_num is not None and tot_num is not None:
        track_val = f"**{pos_num} / {tot_num}**"
    elif player and hasattr(player, "queue"):
        hist_len = len(player.queue.history) if hasattr(player.queue, "history") else 0
        q_len = len(player.queue)
        curr_idx = hist_len + 1
        total_cnt = hist_len + 1 + q_len
        track_val = f"**{curr_idx} / {total_cnt}**"
    else:
        track_val = "**1 / 1**"
    embed.add_field(name="📀 Track", value=track_val, inline=True)

    embed.set_footer(text="Nexus Music Engine • High-Performance Audio")
    return embed




class EffectSelect(discord.ui.Select):

    def __init__(self, player: wavelink.Player | None = None, current_filter: str | None = None):
        self.player = player
        options = [
            discord.SelectOption(label="Clear Filters",  description="Remove all audio effects",      value="clear",     default=(current_filter == "clear")),
            discord.SelectOption(label="Bassboost",      description="Boost the bass frequencies",    value="bassboost", default=(current_filter == "bassboost")),
            discord.SelectOption(label="Lofi / Relax",   description="Calm and relaxing retro vibe",  value="lofi",      default=(current_filter == "lofi")),
            discord.SelectOption(label="Nightcore",      description="Speed up and pitch up",         value="nightcore", default=(current_filter == "nightcore")),
            discord.SelectOption(label="Vaporwave",      description="Slowed and reverb",             value="vaporwave", default=(current_filter == "vaporwave")),
            discord.SelectOption(label="8D Audio",       description="Rotating audio effect",         value="8d",        default=(current_filter == "8d")),
            discord.SelectOption(label="Karaoke",        description="Filter out vocals",             value="karaoke",   default=(current_filter == "karaoke")),
        ]
        # Show the active filter's name in the collapsed dropdown instead of
        # the generic placeholder, so it's clear at a glance what's applied.
        active_labels = {
            "bassboost": "Bassboost", "lofi": "Lofi / Relax", "nightcore": "Nightcore",
            "vaporwave": "Vaporwave", "8d": "8D Audio", "karaoke": "Karaoke",
        }
        placeholder = f"🎚️ Active: {active_labels[current_filter]}" if current_filter in active_labels else "Apply Audio Filter..."
        super().__init__(
            placeholder=placeholder,
            min_values=1, max_values=1,
            options=options,
            custom_id="music_effect_select",
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("❌ Bot is not connected to a voice channel.", ephemeral=True)
        self.player = player

        await interaction.response.defer()
        try:
            val = self.values[0]
            filters = wavelink.Filters()

            if val == "clear":
                self.player.client.get_cog("music")._active_filters.pop(self.player.guild.id, None)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "bassboost":
                # World Recommended Logarithmic 15-Band Bassboost Curve
                filters.equalizer.set(band=0, gain=0.60)
                filters.equalizer.set(band=1, gain=0.50)
                filters.equalizer.set(band=2, gain=0.35)
                filters.equalizer.set(band=3, gain=0.20)
                filters.equalizer.set(band=4, gain=0.10)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "lofi":
                # World Recommended Vintage Low-Pass Lofi Filter
                filters.timescale.set(speed=0.95, pitch=0.95, rate=1.0)
                filters.low_pass.set(smoothing=15.0)
                filters.equalizer.set(band=0, gain=0.25)
                filters.equalizer.set(band=1, gain=0.15)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "nightcore":
                # Real nightcore = speeding the track up, which naturally raises
                # pitch along with it (like a record played faster). speed and
                # pitch are kept equal so the pitch shift matches the speed
                # change instead of sounding artificially over-pitched.
                filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "vaporwave":
                # Real "slowed + reverb" vaporwave = slowing the track down evenly
                # (speed and pitch linked, like a record played slower), plus a
                # low-pass filter to approximate the muffled reverb-like tone
                # Lavalink doesn't have a true reverb filter for.
                filters.timescale.set(speed=0.8, pitch=0.8, rate=1.0)
                filters.low_pass.set(smoothing=12.0)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "8d":
                # World Recommended 3D Surround Binaural Rotation
                filters.rotation.set(rotation_hz=0.25)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)
            elif val == "karaoke":
                # World Recommended Vocal Cancellation + Mid-Band EQ Cut
                filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
                for b in range(4, 9):
                    filters.equalizer.set(band=b, gain=-0.25)
                self.player.client.get_cog("music")._active_filters[self.player.guild.id] = filters
                await self.player.set_filters(filters)

            music_cog = self.player.client.get_cog("music")
            if music_cog is not None:
                if val == "clear":
                    music_cog._active_filter_names.pop(self.player.guild.id, None)
                else:
                    music_cog._active_filter_names[self.player.guild.id] = val

            embed = build_now_playing_embed(self.player)
            view = MusicController(self.player)
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Effect select error: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to apply effect: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to apply effect: {e}", ephemeral=True)


class RadioSelect(discord.ui.Select):

    STATIONS = [
        ("Lofi Girl 24/7 Live",    "24/7 Lofi Hip Hop 🎧 Beats to Relax/Study",          "https://stream.zeno.fm/f3wvbbqmdg8uv"),
        ("Synthwave Radio 24/7",   "24/7 Synthwave & Retrowave beats",                   "https://stream.zeno.fm/0r0xa792kwzuv"),
        ("Chillhop Radio 24/7",    "Jazzy & Lofi Hip Hop Beats 24/7",                    "ytsearch:chillhop lofi hip hop beats"),
        ("Anime Lofi 24/7",        "24/7 Anime Lofi Beats",                               "ytsearch:anime lofi beats relax"),
        ("Jazz Radio 24/7",        "Smooth & Relaxing Coffee Shop Jazz",                  "ytsearch:coffee shop jazz beats"),
        ("Phonk Radio 24/7",       "Drift & Aggressive Phonk Beats 24/7",                 "ytsearch:phonk beats drift music"),
        ("Deep House 24/7",        "Relaxing Deep House & Electronic Radio",              "ytsearch:deep house chill music"),
        ("Hiru FM (Sri Lanka)",    "Sri Lanka's Number 1 Radio Channel",                 "https://radio.lotustechnologieslk.net:2020/stream/hirufmgarden"),
        ("Sirasa FM (Sri Lanka)",  "Sirasa FM Live 24/7",                                "http://live.trusl.com:1170/;"),
        ("FM Derana (Sri Lanka)",  "FM Derana Live Stream",                              "ytsearch:fm derana live radio"),
        ("Shaa FM (Sri Lanka)",    "Shaa FM Sri Lanka Live",                             "ytsearch:shaa fm live"),
        ("Siyatha FM (Sri Lanka)", "Siyatha FM Live Radio",                              "https://srv01.onlineradio.voaplus.com/siyathafm"),
        ("Neth FM (Sri Lanka)",    "Neth FM Sri Lanka Live",                             "https://cp11.serverse.com/proxy/nethfm/stream"),
        ("Shree FM (Sri Lanka)",   "Shree FM Live Stream",                               "ytsearch:shree fm live"),
        ("Lakhanda (Sri Lanka)",   "Lakhanda Radio Live",                                "https://cp12.serverse.com/proxy/itnfm?mp=/stream"),
        ("Ran FM (Sri Lanka)",     "Ran FM Sri Lanka Live",                              "https://a3.asurahosting.com/listen/ranfm/radio.mp3"),
        ("Rangiri (Sri Lanka)",    "Rangiri Sri Lanka Radio",                            "https://rangiri.radioca.st/stream"),
        ("Y FM (Sri Lanka)",       "Y FM Sri Lanka Live Stream",                         "https://mbc.thestreamtech.com:7032/"),
        ("Turn Off Radio",         "Stop the current radio stream",                       "STOP_RADIO"),
    ]

    def __init__(self, player: wavelink.Player | None = None):
        self.player = player
        options = [
            discord.SelectOption(label=name, description=desc, value=url)
            for name, desc, url in self.STATIONS
        ]
        super().__init__(
            placeholder="Select a Radio Station...",
            min_values=1, max_values=1,
            options=options,
            custom_id="music_radio_select",
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("❌ Bot is not connected to a voice channel.", ephemeral=True)
        self.player = player

        await interaction.response.defer()
        url = self.values[0]

        if url == "STOP_RADIO":
            api_cog = self.player.client.get_cog("APICog") or self.player.client.get_cog("api")
            if api_cog and hasattr(api_cog, "restore_saved_playlist") and getattr(self.player, "_saved_playlist_session", None):
                await api_cog.restore_saved_playlist(self.player)
                await interaction.followup.send("📻 Radio stopped • Resumed paused playlist from stop location! 🎵", ephemeral=True)
                embed = build_now_playing_embed(self.player, self.player.current)
                view = MusicController(self.player)
                return await interaction.edit_original_response(embed=embed, view=view)

            await interaction.followup.send("Radio stopped. Bot left the channel.", ephemeral=True)
            try:
                await interaction.message.delete()
            except Exception:
                pass
            if hasattr(self.player, "panel_message"):
                self.player.panel_message = None
            await self.player.disconnect()
            return

        station_name = next((n for n, d, u in self.STATIONS if u == url), "Radio Station")

        try:
            tracks: wavelink.Search = await wavelink.Playable.search(url)
            if not tracks:
                return await interaction.followup.send("Could not connect to this station.", ephemeral=True)

            track = tracks[0] if isinstance(tracks, list) else (tracks.tracks[0] if hasattr(tracks, 'tracks') else tracks)
            setattr(track, "_title_override", station_name)
            setattr(track, "_is_radio", True)

            # SAVE PLAYLIST SESSION BEFORE SWITCHING TO RADIO
            current_track = self.player.current
            is_current_radio = getattr(current_track, "_is_radio", False)
            if not is_current_radio and (current_track or not self.player.queue.is_empty):
                saved_session = {
                    "track": current_track,
                    "position": getattr(self.player, "position", 0),
                    "queue": list(self.player.queue)
                }
                setattr(self.player, "_saved_playlist_session", saved_session)
                logger.info(f"Discord UI saved playlist session: pos={saved_session['position']}, queue_len={len(saved_session['queue'])}")

            self.player.queue.clear()
            await self.player.play(track)

            embed = build_now_playing_embed(self.player, track)
            view = MusicController(self.player)
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Radio station error: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to tune in: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to tune in: {str(e)}", ephemeral=True)


class QueueSelect(discord.ui.Select):

    def __init__(self, player: wavelink.Player | None = None):
        self.player = player
        options = []

        try:
            if player and hasattr(player, "queue"):
                history = getattr(player.queue, "history", None)
                history_list = list(history)[-5:] if history else []
                for i, track in enumerate(reversed(history_list)):
                    raw_title = getattr(track, "_title_override", None) or getattr(track, "title", None) or "Unknown Title"
                    title = (raw_title[:75] + "...") if len(raw_title) > 75 else raw_title
                    raw_author = getattr(track, "author", None) or "Unknown Artist"
                    author = raw_author[:35]
                    options.append(
                        discord.SelectOption(
                            label=f"⏮️ {title}"[:100],
                            description=f"Previous • By {author}"[:100],
                            value=f"hist_{i}"
                        )
                    )

                max_queue_items = max(0, 25 - len(options))
                queue_list = list(player.queue)[:max_queue_items]
                for i, track in enumerate(queue_list):
                    raw_title = getattr(track, "_title_override", None) or getattr(track, "title", None) or "Unknown Title"
                    title = (raw_title[:70] + "...") if len(raw_title) > 70 else raw_title
                    raw_author = getattr(track, "author", None) or "Unknown Artist"
                    author = raw_author[:35]
                    options.append(
                        discord.SelectOption(
                            label=f"⏭️ {i+1}. {title}"[:100],
                            description=f"Upcoming • By {author}"[:100],
                            value=f"queue_{i}"
                        )
                    )
        except Exception as e:
            logger.warning(f"Error populating QueueSelect options: {e}")

        if not options:
            options.append(discord.SelectOption(
                label="Queue & History empty",
                description="Use !play to add songs",
                value="empty"
            ))

        super().__init__(
            placeholder="Select a song from Queue or History...",
            min_values=1, max_values=1,
            options=options[:25],
            custom_id="music_queue_select",
            row=2
        )

    async def callback(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("❌ Bot is not connected to a voice channel.", ephemeral=True)
        self.player = player
        if interaction.message:
            player.panel_message = interaction.message
            music_cog = player.client.get_cog("music") if player.client else None
            if music_cog and interaction.guild:
                music_cog._active_panels[interaction.guild.id] = interaction.message

        val = self.values[0]
        if val == "empty":
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)

        await interaction.response.defer()
        try:
            music_cog = interaction.client.get_cog("music")
            if val.startswith("queue_"):
                index = int(val.split("_")[1])
                queue_list = list(self.player.queue)
                if index >= len(queue_list):
                    return await interaction.followup.send("That track no longer exists in the queue.", ephemeral=True)
                track = queue_list[index]
                del queue_list[index]
                self.player.queue.clear()
                for t in queue_list:
                    await self.player.queue.put_wait(t)
                await self.player.play(track)
                if music_cog and hasattr(music_cog, "_update_panel"):
                    await music_cog._update_panel(self.player, track=track)
                await interaction.followup.send(f"Jumped forward to **{sanitize_md(track.title)}**!", ephemeral=True)
                
            elif val.startswith("hist_"):
                index = int(val.split("_")[1])
                history_list = list(self.player.queue.history)[-5:]
                real_idx = len(history_list) - 1 - index
                
                if real_idx < 0 or real_idx >= len(history_list):
                    return await interaction.followup.send("Track not found in history.", ephemeral=True)
                    
                prev_track = history_list[real_idx]
                
                # Fetch fresh track object
                query = prev_track.uri or prev_track.identifier
                res = await wavelink.Playable.search(query)
                if res:
                    fresh_track = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                    await self.player.play(fresh_track)
                    if music_cog and hasattr(music_cog, "_update_panel"):
                        await music_cog._update_panel(self.player, track=fresh_track)
                    await interaction.followup.send(f"Jumped backward to **{sanitize_md(fresh_track.title)}**!", ephemeral=True)
                else:
                    await interaction.followup.send("Could not load the history track.", ephemeral=True)
        except Exception as e:
            logger.error(f"Queue/History jump error: {e}", exc_info=True)
            await interaction.followup.send(f"Failed: {str(e)}", ephemeral=True)


class SearchSelect(discord.ui.Select):
    def __init__(self, tracks: list[wavelink.Playable], ctx: commands.Context):
        self.tracks = tracks
        self.ctx = ctx
        options = []
        for i, track in enumerate(tracks[:5]):
            dur_sec = int((track.length or 0) / 1000)
            dur_str = f"{dur_sec // 60:02d}:{dur_sec % 60:02d}"
            title = track.title[:80]
            author = track.author[:40]
            options.append(discord.SelectOption(
                label=f"{i+1}. {title}",
                description=f"By {author} • [{dur_str}]",
                value=str(i)
            ))
        super().__init__(
            placeholder="Select a track from search results...",
            min_values=1, max_values=1,
            options=options,
            custom_id="music_search_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        index = int(self.values[0])
        track = self.tracks[index]
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc:
            if interaction.user.voice:
                try:
                    vc = await _connect_voice_with_retry(interaction.user.voice.channel, bot=interaction.client)
                except wavelink.exceptions.InvalidNodeException:
                    return await interaction.followup.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", ephemeral=True)
                except VoiceChannelPermissionError as e:
                    return await interaction.followup.send(f"❌ **Missing voice channel permissions:** {e}", ephemeral=True)
                except Exception as e:
                    return await interaction.followup.send(f"❌ **Voice connection timeout or error:** {e}", ephemeral=True)
                vc.inactive_timeout = None
            else:
                return await interaction.followup.send("You must be in a voice channel first.", ephemeral=True)

        vc.home = interaction.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Prevent auto-disconnect
        was_playing = vc.playing or vc.paused
        await vc.queue.put_wait(track)

        if not was_playing:
            next_track = vc.queue.get()
            try:
                music_cog = self.ctx.bot.get_cog("music")
                if music_cog:
                    vc = await music_cog._safe_play(vc, next_track)
                else:
                    await vc.play(next_track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                if not interaction.is_expired():
                    await interaction.followup.send(f"❌ **Playback couldn't recover:** {e}", ephemeral=True)

        await interaction.followup.send(f"✅ Added **{sanitize_md(track.title)}** to the queue!", ephemeral=True)


class SearchView(discord.ui.View):
    def __init__(self, tracks: list[wavelink.Playable], ctx: commands.Context):
        super().__init__(timeout=30)
        self.add_item(SearchSelect(tracks, ctx))



class SavePlaylistModal(discord.ui.Modal, title='Save Playlist'):
    name = discord.ui.TextInput(
        label='Playlist Name',
        placeholder='My Awesome Vibe...',
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc or not vc.current:
            return await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)
            
        tracks_data = [vc.current.uri]
        for track in vc.queue:
            tracks_data.append(track.uri)
            
        if len(tracks_data) > 100:
            tracks_data = tracks_data[:100]
            
        success = await db._run(db.save_global_playlist, interaction.user.id, self.name.value, tracks_data)
        if success:
            await interaction.response.send_message(f"✅ Saved **{len(tracks_data)}** tracks to `{self.name.value}`!", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ You already have a playlist named `{self.name.value}`. Delete it first.", ephemeral=True)

class LoadPlaylistSelect(discord.ui.Select):
    def __init__(self, playlists: list[str], page: int):
        self.page = page
        start = page * 25
        end = start + 25
        opts = playlists[start:end]
        options = [discord.SelectOption(label=p, value=p, description="Load this playlist") for p in opts]
        if not options:
            options = [discord.SelectOption(label="No playlists found", value="none")]
        super().__init__(placeholder=f"📂 Load a Playlist (Page {page+1})...", min_values=1, max_values=1, options=options, custom_id="pl_load")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.defer(ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        name = self.values[0]
        payloads = await db._run(db.load_global_playlist, interaction.user.id, name)
        if not payloads:
            return await interaction.followup.send(f"❌ Playlist `{name}` not found.", ephemeral=True)
            
        if not interaction.user.voice:
            return await interaction.followup.send("❌ You need to be in a voice channel.", ephemeral=True)
            
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc:
            try:
                vc = await _connect_voice_with_retry(interaction.user.voice.channel, bot=interaction.client)
            except wavelink.exceptions.InvalidNodeException:
                return await interaction.followup.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", ephemeral=True)
            except VoiceChannelPermissionError as e:
                return await interaction.followup.send(f"❌ **Missing voice channel permissions:** {e}", ephemeral=True)
            except Exception as e:
                return await interaction.followup.send(f"❌ **Voice connection timeout or error:** {e}", ephemeral=True)
            vc.home = interaction.channel
            vc.autoplay = wavelink.AutoPlayMode.disabled
            vc.inactive_timeout = None  # Prevent auto-disconnect
            
        await interaction.followup.send(f"⚡ Loading {len(payloads)} tracks from `{name}` instantly...", ephemeral=True)
        
        count = 0
        for payload in payloads:
            try:
                results = await wavelink.Playable.search(payload)
                if results:
                    track = results[0] if isinstance(results, list) else (results.tracks[0] if hasattr(results, 'tracks') else results)
                    await vc.queue.put_wait(track)
                    count += 1
            except Exception:
                pass
                
        if not (vc.playing or vc.paused) and not vc.queue.is_empty:
            next_track = vc.queue.get()
            try:
                music_cog = interaction.client.get_cog("music")
                if music_cog:
                    vc = await music_cog._safe_play(vc, next_track)
                else:
                    await vc.play(next_track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                if not interaction.is_expired():
                    await interaction.followup.send(f"❌ **Playback couldn't recover:** {e}", ephemeral=True)
            
        await interaction.edit_original_response(content=f"✅ Lightning loaded **{count}** tracks from global playlist `{name}`!")

class DeletePlaylistSelect(discord.ui.Select):
    def __init__(self, playlists: list[str], page: int):
        self.page = page
        start = page * 25
        end = start + 25
        opts = playlists[start:end]
        options = [discord.SelectOption(label=p, value=p, description="Delete this playlist", emoji="🗑️") for p in opts]
        if not options:
            options = [discord.SelectOption(label="No playlists found", value="none")]
        super().__init__(placeholder=f"🗑️ Delete a Playlist (Page {page+1})...", min_values=1, max_values=1, options=options, custom_id="pl_delete")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.defer()
        name = self.values[0]
        deleted = await db._run(db.delete_global_playlist, interaction.user.id, name)
        if deleted:
            await interaction.response.send_message(f"✅ Deleted playlist `{name}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Playlist `{name}` not found.", ephemeral=True)

class AddToPlaylistSelect(discord.ui.Select):
    def __init__(self, playlists: list[str], page: int):
        self.page = page
        start = page * 25
        end = start + 25
        opts = playlists[start:end]
        options = [discord.SelectOption(label=p, value=p, description="Add current song to this playlist", emoji="➕") for p in opts]
        if not options:
            options = [discord.SelectOption(label="No playlists found", value="none")]
        super().__init__(placeholder=f"➕ Add current song to... (Page {page+1})", min_values=1, max_values=1, options=options, custom_id="pl_add")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.defer(ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc or not vc.current:
            return await interaction.followup.send("❌ Nothing is playing right now.", ephemeral=True)
            
        name = self.values[0]
        success = await db._run(db.append_to_global_playlist, interaction.user.id, name, vc.current.uri)
        
        if success:
            await interaction.followup.send(f"✅ Added **{getattr(vc.current, '_title_override', vc.current.title)}** to playlist `{name}`!", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed to add to `{name}`.", ephemeral=True)

class PlaylistView(discord.ui.View):
    def __init__(self, playlists: list[str], page: int = 0):
        super().__init__(timeout=60)
        self.playlists = playlists
        self.page = page
        self.max_page = max(0, (len(playlists) - 1) // 25)
        
        if playlists:
            self.add_item(LoadPlaylistSelect(playlists, page))
            self.add_item(AddToPlaylistSelect(playlists, page))
            self.add_item(DeletePlaylistSelect(playlists, page))
            
        # Pagination buttons
        if self.max_page > 0:
            btn_prev = discord.ui.Button(label="◀️ Prev", style=discord.ButtonStyle.secondary, disabled=(self.page == 0))
            btn_next = discord.ui.Button(label="Next ▶️", style=discord.ButtonStyle.secondary, disabled=(self.page == self.max_page))
            
            async def prev_callback(i: discord.Interaction):
                await i.response.edit_message(view=PlaylistView(self.playlists, self.page - 1))
            async def next_callback(i: discord.Interaction):
                await i.response.edit_message(view=PlaylistView(self.playlists, self.page + 1))
                
            btn_prev.callback = prev_callback
            btn_next.callback = next_callback
            self.add_item(btn_prev)
            self.add_item(btn_next)

    @discord.ui.button(label="💾 Save Current Queue (As New Playlist)", style=discord.ButtonStyle.success, custom_id="pl_save")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SavePlaylistModal())

class MusicController(discord.ui.View):
    def __init__(self, player: wavelink.Player | None = None):
        super().__init__(timeout=None)
        self.player = player

        current_filter = None
        if player and getattr(player, "guild", None):
            music_cog = player.client.get_cog("music") if player.client else None
            if music_cog is not None:
                current_filter = music_cog._active_filter_names.get(player.guild.id)

        self.add_item(EffectSelect(player, current_filter=current_filter))
        self.add_item(RadioSelect(player))
        self.add_item(QueueSelect(player))
        
        # Dynamically set Autoplay button style from per-guild flag
        is_ap = False
        if player and player.guild:
            client = getattr(player, "client", None)
            cog = client.get_cog("music") if client else None
            if cog and hasattr(cog, "_spotify_autoplay_enabled"):
                is_ap = cog._spotify_autoplay_enabled.get(player.guild.id, False)

        if is_ap:
            self.autoplay_btn.style = discord.ButtonStyle.success
            self.autoplay_btn.label = "Autoplay"
        else:
            self.autoplay_btn.style = discord.ButtonStyle.secondary
            self.autoplay_btn.label = "Autoplay"
            
    

    # ── Row 3: First Row (5 Buttons) ───────────────────────────────────────

    @discord.ui.button(label="", emoji="⏪", style=discord.ButtonStyle.primary, custom_id="music_rewind10", row=3)
    async def rewind_10s(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player) or not player.current:
            return await interaction.response.send_message("Not connected or nothing playing.", ephemeral=True)
        self.player = player
        
        new_pos = max(0, (player.position or 0) - 10000)
        await player.seek(new_pos)
        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="", emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="music_voldown", row=3)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        self.player = player
        
        new_vol = max(0, player.volume - 10)
        await player.set_volume(new_vol)
        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.secondary, custom_id="music_autoplay", row=3)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        self.player = player

        guild_id = player.guild.id if player.guild else None
        client = getattr(player, "client", None)
        cog = client.get_cog("music") if client else None

        current_ap = False
        if cog and hasattr(cog, "_spotify_autoplay_enabled") and guild_id:
            current_ap = cog._spotify_autoplay_enabled.get(guild_id, False)
            cog._spotify_autoplay_enabled[guild_id] = not current_ap
            new_ap = not current_ap
        else:
            new_ap = False

        # Keep wavelink player.autoplay permanently disabled
        player.autoplay = wavelink.AutoPlayMode.disabled

        if new_ap:
            button.style = discord.ButtonStyle.success
            button.label = "Autoplay"
        else:
            button.style = discord.ButtonStyle.secondary
            button.label = "Autoplay"
        
        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="", emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music_volup", row=3)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        self.player = player
        
        new_vol = min(100, player.volume + 10)
        await player.set_volume(new_vol)
        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="", emoji="⏩", style=discord.ButtonStyle.primary, custom_id="music_forward10", row=3)
    async def forward_10s(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player) or not player.current:
            return await interaction.response.send_message("Not connected or nothing playing.", ephemeral=True)
        self.player = player
        
        new_pos = min(player.current.length, (player.position or 0) + 10000)
        await player.seek(new_pos)
        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    # ── Row 4: Second Row (5 Buttons) ──────────────────────────────────────

    @discord.ui.button(label="", emoji="⏮️", style=discord.ButtonStyle.primary, custom_id="music_back", row=4)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
        self.player = player
        if interaction.message:
            player.panel_message = interaction.message
            music_cog = player.client.get_cog("music") if player.client else None
            if music_cog and interaction.guild:
                music_cog._active_panels[interaction.guild.id] = interaction.message

        await interaction.response.defer()
        music_cog = player.client.get_cog("music") if player.client else None
        if music_cog and hasattr(music_cog, "_execute_previous"):
            await music_cog._execute_previous(player)

    @discord.ui.button(label="", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music_pause", row=4)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("❌ Not connected to a voice channel.", ephemeral=True)
        self.player = player
        
        if player.paused:
            await player.pause(False)
        elif player.playing:
            await player.pause(True)
        elif not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
        else:
            return await interaction.response.send_message("Queue is empty. Use `!play <song>` to start playing!", ephemeral=True)

        embed = build_now_playing_embed(player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Stop Bot", style=discord.ButtonStyle.danger, custom_id="music_stop", row=4)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected.", ephemeral=True)

        if interaction.guild and hasattr(player, "client"):
            music_cog = player.client.get_cog("music") or player.client.get_cog("Music")
            if music_cog and hasattr(music_cog, "save_session_for_guild"):
                try:
                    await music_cog.save_session_for_guild(interaction.guild.id, player)
                except Exception:
                    pass

        await interaction.response.send_message("Stopped and left the channel.", ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
            
        if hasattr(player, "panel_message"):
            player.panel_message = None
            
        await player.disconnect()
        self.stop()

    @discord.ui.button(label="", emoji="🔀", style=discord.ButtonStyle.primary, custom_id="music_shuffle", row=4)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty. Nothing to shuffle.", ephemeral=True)
            
        try:
            player.queue.shuffle()
            embed = build_now_playing_embed(player)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"Error shuffling queue: {e}", ephemeral=True)

    @discord.ui.button(label="", emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="music_skip", row=4)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = interaction.guild.voice_client if interaction.guild else self.player
        if not _player_valid(player):
            return await interaction.response.send_message(
                "Not connected. If the bot recently recovered from a connection issue, "
                "try re-opening the player panel with `!nowplaying` or your play command.",
                ephemeral=True
            )
        self.player = player
        if interaction.message:
            player.panel_message = interaction.message
            music_cog = player.client.get_cog("music") if player.client else None
            if music_cog and interaction.guild:
                music_cog._active_panels[interaction.guild.id] = interaction.message

        await interaction.response.defer()
        music_cog = player.client.get_cog("music") if player.client else None
        if music_cog and hasattr(music_cog, "_execute_skip"):
            await music_cog._execute_skip(player)
        else:
            await player.skip(force=True)














# ── Cog ───────────────────────────────────────────────────────────────────────

class MusicCog(commands.Cog, name="music"):
    async def _connect_voice_with_retry(
        self,
        channel: discord.VoiceChannel,
        retries: int = 2,
        timeout: float = 15.0
    ) -> wavelink.Player:
        return await _connect_voice_with_retry(channel, retries=retries, timeout=timeout, bot=self.bot)

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        # Track guild_id → channel used for 24/7 mode so we can re-queue
        self._247_guilds: set[int] = set()
        # Track guild_id → active panel message to cleanly delete on disconnect
        self._active_panels: dict[int, discord.Message] = {}
        # State tracking for recovery and queue advancement
        self._recovery_locks: dict[int, asyncio.Lock] = {}
        self._advance_locks: dict[int, asyncio.Lock] = {}
        self._spotify_autoplay_enabled: dict[int, bool] = {}
        self._consecutive_fails: dict[int, int] = {}
        self._active_filters: dict[int, wavelink.Filters] = {}
        # Tracks which filter *name* (e.g. "bassboost") is active per guild,
        # so the Effect dropdown can show it as the selected option instead
        # of always resetting to the generic placeholder.
        self._active_filter_names: dict[int, str] = {}
        # Explicit track-position tracking. Previously the "Track X / Y"
        # display was derived by counting len(queue.history) + len(queue) at
        # embed-render time — fragile under concurrent modification (Back's
        # background requeue task, panel auto-refresh on track_start, and
        # history dedup all race to mutate/read the same queue at slightly
        # different times), which is what caused inconsistent counts.
        # Tracked explicitly instead: set on playlist load, incremented on
        # every forward track_start, decremented explicitly by Back.
        self._track_position: dict[int, int] = {}
        self._track_total: dict[int, int] = {}
        # Set by Back right before it calls play(), so the track_start
        # handler knows this particular start was a "go back" (already
        # explicitly decremented) and should NOT also increment — otherwise
        # Back's own track_start would double-count against its decrement.
        self._suppress_next_increment: set[int] = set()
        # Ring buffer of recent stale-session recovery / track-stuck-skip
        # events, exposed via the dashboard's /api/recovery-events endpoint.
        self.recovery_events: collections.deque = collections.deque(maxlen=50)
        self._recovering_guilds: set[int] = set()

    def _get_advance_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._advance_locks:
            self._advance_locks[guild_id] = asyncio.Lock()
        return self._advance_locks[guild_id]

    @property
    def http_session(self) -> aiohttp.ClientSession:
        if hasattr(self.bot, "session") and self.bot.session and not self.bot.session.closed:
            return self.bot.session
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _safe_play(self, vc: wavelink.Player, track: wavelink.Playable, retries=2) -> wavelink.Player:
        guild_id = vc.guild.id
        if guild_id not in self._recovery_locks:
            self._recovery_locks[guild_id] = asyncio.Lock()
            
        for attempt in range(retries):
            try:
                await vc.play(track)
                return vc
            except wavelink.exceptions.LavalinkException as e:
                logger.warning(f"LavalinkException on play for guild {guild_id}: {e}. Retrying ({attempt+1}/{retries})...")
                await asyncio.sleep(2)
                
                if attempt == retries - 1:
                    logger.error(f"Failed to play track after {retries} attempts. Lavalink node may be blocked by YouTube.")
                    if hasattr(vc, "home") and vc.home:
                        try:
                            await vc.home.send(f"⚠️ **Could not play that track!** The public Lavalink node is likely being blocked by YouTube. Try another song or a Spotify link.", delete_after=10)
                        except Exception:
                            pass
                    # We do NOT disconnect the bot here anymore. Just raise so the caller knows it failed.
                    raise e
        return vc

    async def _recover_stale_player(self, vc: wavelink.Player, guild_id: int):
        import asyncio
        async with self._recovery_locks[guild_id]:
            try:
                self._recovering_guilds.add(guild_id)
                channel = getattr(vc, "channel", None)
                if not channel:
                    return
                
                current_track = getattr(vc, "current", None)
                queue_copy = list(vc.queue) if hasattr(vc, "queue") else []
                position = getattr(vc, "position", 0)
                volume = getattr(vc, "volume", 100)
                filters = self._active_filters.get(guild_id)
                
                logger.info(f"Recovering stale player for guild {guild_id}")
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                    
                new_vc = await _connect_voice_with_retry(channel, bot=self.bot)
                new_vc.home = getattr(vc, "home", None)
                new_vc.panel_message = getattr(vc, "panel_message", None)
                new_vc.autoplay = getattr(vc, "autoplay", wavelink.AutoPlayMode.disabled)
                if new_vc.autoplay == wavelink.AutoPlayMode.disabled:
                    new_vc.autoplay = wavelink.AutoPlayMode.disabled
                await new_vc.set_volume(volume)
                if filters:
                    await new_vc.set_filters(filters)
                    
                for t in queue_copy:
                    new_vc.queue.put(t)
                    
                if current_track:
                    self._suppress_next_increment.add(guild_id)
                    await new_vc.play(current_track)
                    if position > 0:
                        await new_vc.seek(position)
                        logger.info(f"Explicitly called vc.seek({position}) after reconnecting to restore track position.")

                await self._refresh_panel_player(guild_id, new_vc)
                self._log_recovery_event(guild_id, "stale_recovery", success=True)
            except Exception as e:
                logger.error(f"Failed to recover stale player for guild {guild_id}: {e}")
                self._log_recovery_event(guild_id, "stale_recovery", success=False)
            finally:
                self._recovering_guilds.discard(guild_id)

    def _log_recovery_event(self, guild_id: int, event: str, success: bool):
        """Record a stale-recovery / track-stuck-skip event for the
        dashboard's Recent Recovery Events feed."""
        guild = self.bot.get_guild(guild_id)
        self.recovery_events.append({
            "timestamp": time.time(),
            "guild_id": str(guild_id),
            "guild_name": guild.name if guild else "Unknown",
            "event": event,
            "success": success,
        })

    async def _refresh_panel_player(self, guild_id: int, new_vc: wavelink.Player):
        """
        CRITICAL after any stale-session recovery: the MusicController view
        attached to the existing Now Playing panel message still holds a
        reference to the OLD (now-disconnected) wavelink.Player object via
        `self.player`. Every button (Back, Skip, volume, etc.) on that panel
        would keep acting on the dead player — clicks would silently fail or
        do nothing — until the panel is rebuilt against the NEW player.
        This re-renders the existing panel message with a fresh view bound
        to new_vc, so buttons work again immediately after a recovery.
        """
        panel_msg = self._active_panels.get(guild_id)
        if not panel_msg:
            return
        try:
            embed = build_now_playing_embed(new_vc)
            view = MusicController(new_vc)
            await panel_msg.edit(embed=embed, view=view)
            self._active_panels[guild_id] = panel_msg
            logger.info(f"Refreshed Now Playing panel buttons for guild {guild_id} after recovery.")
        except Exception as e:
            logger.warning(f"Failed to refresh panel after recovery for guild {guild_id}: {e}")

    @commands.Cog.listener()

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        # Check if they just uploaded an audio file without a command
        if message.attachments and not message.content.startswith(self.bot.command_prefix):
            for att in message.attachments:
                if (att.content_type and att.content_type.startswith("audio/")) or att.filename.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac')):
                    if message.author.voice and message.author.voice.channel:
                        ctx = await self.bot.get_context(message)
                        await self.play_attachment(ctx, att)
                        return

    async def play_attachment(self, ctx: commands.Context, att: discord.Attachment):
        channel = await _ensure_author_in_voice(ctx)
        if not channel:
            return

        vc: wavelink.Player = ctx.voice_client
        if vc and not getattr(vc, "channel", None):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None

        if not vc:
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)

        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Prevent auto-disconnect
        
        status_msg = await ctx.send("🔊 **Interrupting** to play voice clip...")
        
        try:
            tracks = await wavelink.Playable.search(att.url)
            if not tracks:
                return await status_msg.edit(content="❌ Could not play that file.")
                
            track = tracks[0] if isinstance(tracks, list) else (tracks.tracks[0] if hasattr(tracks, 'tracks') else tracks)
            
            # ── Override "Unknown title" with the actual file name ──
            clean_fname = sanitize_md(att.filename)
            setattr(track, "_title_override", clean_fname)
            
            if vc.current:
                # Put the currently playing song back at index 0 so it plays right after the clip!
                vc.queue.put_at(0, vc.current)
                
            try:
                vc = await self._safe_play(vc, track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                await ctx.send(f"❌ **Playback couldn't recover:** {e}")
            await status_msg.edit(content=f"▶️ **Playing Instantly:** {clean_fname}")
        except Exception as e:
            await status_msg.edit(content=f"❌ Error playing attachment: {e}")

    async def _update_panel(self, player: wavelink.Player, track: wavelink.Playable | None = None) -> None:
        """
        Unified panel updater that guarantees the Now Playing embed and controller view
        are edited in-place instantly without lag or desync.
        """
        if not player or not player.guild:
            return
        guild_id = player.guild.id
        curr_track = track or getattr(player, "current", None)
        try:
            embed = build_now_playing_embed(player, curr_track)
            view = MusicController(player)
            
            panel = getattr(player, "panel_message", None) or self._active_panels.get(guild_id)
            if panel:
                try:
                    await panel.edit(embed=embed, view=view)
                    player.panel_message = panel
                    self._active_panels[guild_id] = panel
                    return
                except discord.NotFound:
                    player.panel_message = None
                    self._active_panels.pop(guild_id, None)
                except discord.HTTPException as e:
                    logger.warning(f"Failed to edit panel in guild {guild_id}: {e}")
            
            home = getattr(player, "home", None)
            if home:
                new_msg = await home.send(embed=embed, view=view)
                player.panel_message = new_msg
                self._active_panels[guild_id] = new_msg
        except Exception as e:
            logger.error(f"Error in _update_panel for guild {guild_id}: {e}", exc_info=True)
        finally:
            api_cog = self.bot.get_cog("APICog") or self.bot.get_cog("api")
            if api_cog and hasattr(api_cog, "broadcast_state"):
                asyncio.create_task(api_cog.broadcast_state(guild_id))

    async def _ensure_queue_advance(self, player: wavelink.Player, guild_id: int):
        """
        AutoPlay safety net: If a track ended but the next song did not start
        automatically within 1.5 seconds, advance the queue and start the next song.
        """
        await asyncio.sleep(1.5)
        if not _player_valid(player):
            return
        async with self._get_advance_lock(guild_id):
            if not player.playing and not player.paused and not player.queue.is_empty:
                logger.info(f"AutoPlay safety net: Queue not empty but player idle in guild {guild_id}. Starting next song automatically...")
                try:
                    next_track = player.queue.get()
                    player.autoplay = wavelink.AutoPlayMode.disabled
                    self._consecutive_fails[guild_id] = 0

                    if guild_id in self._track_position:
                        self._track_position[guild_id] += 1
                        if self._track_position[guild_id] > self._track_total.get(guild_id, 0):
                            self._track_total[guild_id] = self._track_position[guild_id]
                    self._suppress_next_increment.add(guild_id)

                    await player.play(next_track)
                    await self._update_panel(player, track=next_track)
                except Exception as e:
                    logger.error(f"AutoPlay safety net failed to start next track for guild {guild_id}: {e}", exc_info=True)

    @tasks.loop(seconds=5)
    async def _queue_watchdog_loop(self):
        """
        Continuous background watchdog:
        Ensures that if a player is idle, or stalled (position unchanged for 15s)
        while songs are waiting in queue, the next song is played automatically.
        """
        try:
            for vc in list(self.bot.voice_clients):
                if not isinstance(vc, wavelink.Player) or not _player_valid(vc) or not vc.guild:
                    continue
                if getattr(vc, "_skip_lock", False) or getattr(vc, "_prev_lock", False):
                    continue
                guild_id = vc.guild.id
                
                # Check for stalled playback
                now = time.time()
                is_stalled = False
                
                if vc.playing and not vc.paused:
                    current_pos = getattr(vc, "position", 0)
                    last_pos = getattr(vc, "_watchdog_last_pos", -1)
                    last_pos_time = getattr(vc, "_watchdog_last_pos_time", 0)
                    
                    if current_pos == last_pos:
                        if now - last_pos_time > 15.0:
                            is_stalled = True
                    else:
                        vc._watchdog_last_pos = current_pos
                        vc._watchdog_last_pos_time = now

                if (not vc.playing and not vc.paused and not vc.queue.is_empty) or is_stalled:
                    idle_start = getattr(vc, "_idle_since", None)
                    if idle_start is None:
                        vc._idle_since = now
                    elif now - idle_start >= 3.0:
                        reason = "STALLED" if is_stalled else "IDLE"
                        logger.warning(f"Queue watchdog: Player {reason} in guild {guild_id}. Force advancing queue...")
                        vc._idle_since = None
                        vc._watchdog_last_pos_time = now  # Reset to prevent spam
                        try:
                            async with self._get_advance_lock(guild_id):
                                if not vc.queue.is_empty and not vc.playing:
                                    next_track = vc.queue.get()
                                    self._consecutive_fails[guild_id] = 0

                                    if guild_id in self._track_position:
                                        self._track_position[guild_id] += 1
                                        if self._track_position[guild_id] > self._track_total.get(guild_id, 0):
                                            self._track_total[guild_id] = self._track_position[guild_id]
                                    self._suppress_next_increment.add(guild_id)

                                    await vc.play(next_track)
                                    asyncio.create_task(self._update_panel(vc, track=next_track))
                                elif vc.queue.is_empty and is_stalled:
                                    await vc.stop()
                        except Exception as e:
                            logger.error(f"Queue watchdog failed to start next track: {e}")
                else:
                    vc._idle_since = None
        except Exception as e:
            logger.debug(f"Queue watchdog loop error: {e}")

    def cog_unload(self) -> None:
        if self._queue_watchdog_loop.is_running():
            self._queue_watchdog_loop.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    async def cog_load(self) -> None:
        try:
            self.bot.add_view(MusicController(None))
        except Exception as e:
            logger.warning(f'Could not register persistent MusicController view: {e}')

        if not self._queue_watchdog_loop.is_running():
            self._queue_watchdog_loop.start()

        async def _fetch_public_nodes() -> list[wavelink.Node]:
            """
            Public fallback node — NOT connected by default. Previously,
            public nodes were connected alongside the private node
            simultaneously, and wavelink's own load-balancing sometimes
            routed players to the public node even while the private node
            was healthy, causing silent voice drops. This is now only
            connected on-demand (see on_wavelink_node_closed below) when
            the private node is genuinely unreachable, and disconnected
            again the moment the private node comes back — so during
            normal operation, the private node remains the ONLY node in
            the Pool, exactly as before.
            """
            return []

        def _build_public_backup_nodes() -> list[wavelink.Node]:
            """
            Public Lavalink nodes fallback.
            Note: No public backup nodes are currently configured (configs = []).
            All playback relies on the configured private Lavalink node(s).
            """
            configs = []
            nodes = []
            for identifier, host, port, password, secure in configs:
                try:
                    uri_scheme = "https" if secure else "http"
                    nodes.append(wavelink.Node(
                        uri=f"{uri_scheme}://{host}:{port}",
                        password=password,
                        identifier=identifier,
                    ))
                except Exception:
                    continue
            return nodes

        try:
            self._public_backup_nodes = _build_public_backup_nodes()
        except Exception as e:
            logger.error(f"❌ Failed to build public backup node list (non-fatal, continuing without backups): {e}", exc_info=True)
            self._public_backup_nodes = []

        async def _connect_nodes():
            lavalink_host   = os.getenv("LAVALINK_HOST")
            lavalink_port   = os.getenv("LAVALINK_PORT")
            lavalink_pass   = os.getenv("LAVALINK_PASSWORD")
            
            if not lavalink_host or not lavalink_port or not lavalink_pass:
                logger.critical(
                    "❌ LAVALINK_HOST / LAVALINK_PORT / LAVALINK_PASSWORD are not "
                    "fully set in .env — cannot connect to Lavalink. Music commands "
                    "will not work until these are configured."
                )
                return
            
            # Format the URI
            private_uri = f"http://{lavalink_host}:{lavalink_port}"

            # ── STEP 1: Try private node first with a strict 12-second timeout ──
            # If OptikLink's firewall is blocking the port, the default TCP
            # timeout is ~2 minutes which freezes everything. We abort fast
            # and fall through to public backups so music keeps working.
            private_node = wavelink.Node(
                uri=private_uri,
                password=lavalink_pass,
                identifier=PRIMARY_NODE_ID
            )
            
            # ── STEP 1.5: Optional Secondary Node ──
            secondary_host = os.getenv("LAVALINK2_HOST")
            secondary_port = os.getenv("LAVALINK2_PORT")
            secondary_pass = os.getenv("LAVALINK2_PASSWORD")
            secondary_node = None
            if secondary_host and secondary_port and secondary_pass:
                secondary_node = wavelink.Node(
                    uri=f"http://{secondary_host}:{secondary_port}",
                    password=secondary_pass,
                    identifier="Secondary-Node"
                )

            private_connected = False
            try:
                await asyncio.wait_for(
                    wavelink.Pool.connect(nodes=[private_node], client=self.bot, cache_capacity=100),
                    timeout=12.0
                )
                private_connected = True
                logger.info(f"✅ Connected to private primary node: Primary-Node ({private_uri})")
            except asyncio.TimeoutError:
                logger.warning(
                    f"⚠️ Primary node timed out after 12s ({private_uri}) — "
                    "OptikLink firewall may be blocking the port. "
                    "No public backup nodes configured."
                )
            except Exception as e:
                logger.warning(f"⚠️ Primary node failed ({private_uri}): {e} — no public backup nodes configured.")

            secondary_connected = False
            if secondary_node:
                try:
                    await asyncio.wait_for(
                        wavelink.Pool.connect(nodes=[secondary_node], client=self.bot, cache_capacity=100),
                        timeout=12.0
                    )
                    secondary_connected = True
                    logger.info(f"✅ Connected to private secondary node: Secondary-Node ({secondary_node.uri})")
                except asyncio.TimeoutError:
                    logger.warning(f"⚠️ Secondary node timed out after 12s ({secondary_node.uri}) — primary/backup nodes unaffected.")
                except Exception as e:
                    logger.warning(f"⚠️ Secondary node failed ({secondary_node.uri}): {e} — primary/backup nodes unaffected.")

            # 🛠️ STEP 2: Always also connect public backups for resilience 🛠️🛠️🛠️🛠️🛠️
            # Fetch extra nodes from community API
            api_nodes = await _fetch_public_nodes()

            # Add the hardcoded public backups too
            public_fallback_nodes = self._public_backup_nodes + api_nodes

            connected = (1 if private_connected else 0) + (1 if secondary_connected else 0)
            failed_nodes = []

            for node in public_fallback_nodes:
                try:
                    await asyncio.wait_for(
                        wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100),
                        timeout=10.0
                    )
                    connected += 1
                    logger.info(f"✅ Connected public backup: {node.identifier} ({node.uri})")
                except asyncio.TimeoutError:
                    failed_nodes.append((node.identifier, "Timed out (10s)"))
                    logger.warning(f"⚠️ Skipped {node.identifier}: timed out")
                except Exception as e:
                    failed_nodes.append((node.identifier, str(e)))
                    logger.warning(f"⚠️ Skipped {node.identifier}: {e}")

            if connected == 0:
                logger.critical("❌ CRITICAL: No Lavalink nodes connected! Bot music will not work!")
            else:
                logger.info(f"✓ Lavalink pool active: {connected} node(s) operational.")
            total = 1 + (1 if secondary_node else 0) + len(public_fallback_nodes)
            logger.info(f"Wavelink startup complete: {connected}/{total} node(s) connected (no public failover configured).")
            
            if failed_nodes:
                logger.info("Failed nodes:")
                for node_id, error in failed_nodes:
                    logger.info(f"  • {node_id}: {error}")



        # Run in background so bot startup is NOT blocked
        asyncio.create_task(_connect_nodes())
        logger.info("Wavelink node connection started in background.")



    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        """
        Handles track completion:
        1. Resets fail counters on successful completion.
        2. Ensures autoplay remains active and error counts are clean.
        3. Spawns an auto-advance safety check so the next song is NEVER missed.
        4. Handles 24/7 re-queue and Spotify autoplay fallbacks.
        """
        player: wavelink.Player = payload.player
        if not player or not player.guild:
            return

        guild_id = player.guild.id
        reason = getattr(payload, "reason", "")

        api_cog = self.bot.get_cog("APICog") or self.bot.get_cog("api")
        if api_cog and hasattr(api_cog, "broadcast_state"):
            asyncio.create_task(api_cog.broadcast_state(guild_id))

        if reason == "finished":
            if hasattr(self, "_consecutive_fails") and guild_id in self._consecutive_fails:
                self._consecutive_fails[guild_id] = 0

        # Keep player.autoplay permanently disabled
        player.autoplay = wavelink.AutoPlayMode.disabled

                # ── MANUAL AUTOPLAY & SAFETY WATCHDOG ──
        if reason != "replaced":
            if payload.track and hasattr(player.queue, "history"):
                try:
                    player.queue.history.put(payload.track)
                except Exception:
                    pass
            
            # BUG FIX: Prevent infinite skip loop on IP ban / IP Block
            if reason in ("loadFailed", "trackException"):
                if not hasattr(self, "_consecutive_fails"):
                    self._consecutive_fails = {}
                self._consecutive_fails[guild_id] = self._consecutive_fails.get(guild_id, 0) + 1
                
                if self._consecutive_fails[guild_id] > 3:
                    logger.error(f"🚨 Stopping runaway auto-skip in {guild_id} (YouTube IP blocked!)")
                    self._consecutive_fails[guild_id] = 0
                    if hasattr(player, "home") and player.home:
                        import asyncio
                        asyncio.create_task(player.home.send(
                            "⚠️ **Playback Paused:** YouTube is actively blocking this server from streaming (`Sign in to confirm you're not a bot`).\n"
                            "Auto-skipping was halted immediately to protect your remaining queue.\n\n"
                            "💡 **Fix:** Check your Lavalink Console. If it says `Please go to https://www.google.com/device and enter code`, do that. "
                            "Otherwise, you need a new Google OAuth token in your application.yml!"
                        ))
                    return # Stop pulling tracks from the queue!
            else:
                if hasattr(self, "_consecutive_fails"):
                    self._consecutive_fails[guild_id] = 0
            
            async with self._get_advance_lock(guild_id):
                if not player.queue.is_empty and not player.playing:
                    # Small delay to throttle requests
                    if reason in ("loadFailed", "trackException"):
                        await asyncio.sleep(1.0)
                    
                    next_track = player.queue.get()
                    logging.getLogger("nexus.music").info(f"Manual AutoPlay: Starting next track '{getattr(next_track, 'title', 'Unknown')}' in guild {guild_id}")
                    self._consecutive_fails[guild_id] = 0
                    
                    if guild_id in self._track_position:
                        self._track_position[guild_id] += 1
                        if self._track_position[guild_id] > self._track_total.get(guild_id, 0):
                            self._track_total[guild_id] = self._track_position[guild_id]
                    self._suppress_next_increment.add(guild_id)
                    
                    try:
                        await player.play(next_track)
                        asyncio.create_task(self._update_panel(player, track=next_track))
                    except Exception as e:
                        logging.getLogger("nexus.music").error(f"Manual AutoPlay failed to start track: {e}")
                        asyncio.create_task(self._ensure_queue_advance(player, guild_id))

        # BUG FIX #7: If the guild is in 24/7 mode and the queue is now empty
        # (track finished and nothing queued), re-search and re-queue lofi.
        if (
            reason != "replaced"
            and guild_id in self._247_guilds
            and player.queue.is_empty
            and not player.playing
        ):
            logger.info("24/7 mode: re-queuing lofi stream.")
            try:
                # ⚠️ PRIORITY: Zeno.fm FIRST (no auth token expiry), YouTube SECOND
                LOFI_SOURCES = [
                    "https://stream.zeno.fm/f3wvbbqmdg8uv",           # True Infinite MP3
                    "https://www.youtube.com/watch?v=X4VbdwhkE10",   # YouTube live fallback
                    "ytsearch:lofi girl chill beats study",             # Last resort
                ]
                requeue_track = None
                for source in LOFI_SOURCES:
                    try:
                        results = await wavelink.Playable.search(source)
                        if results:
                            requeue_track = results[0] if isinstance(results, list) else (results.tracks[0] if hasattr(results, 'tracks') and results.tracks else results)
                            break
                    except Exception:
                        continue

                if requeue_track:
                    await player.play(requeue_track)
                    await self._update_panel(player, track=requeue_track)
                    logger.info("24/7 re-queue successful.")
            except Exception as e:
                logger.error(f"24/7 re-queue failed: {e}")
                
        # Spotify Autoplay Logic
        elif (
            reason != "replaced"
            and player.queue.is_empty
            and not player.playing
            and guild_id not in self._247_guilds
            and self._spotify_autoplay_enabled.get(guild_id, False)
        ):
            history = getattr(player.queue, 'history', None)
            if history and not history.is_empty:
                logger.info("Attempting Spotify Seeded Autoplay...")
                recent = list(history)[-5:]
                seed_tracks = []
                for t in recent:
                    if hasattr(t, "spotify_uri") and t.spotify_uri:
                        seed_tracks.append(t.spotify_uri)
                    else:
                        clean_title = (t.title or "").replace("(Official Video)", "").replace("[Official Music Video]", "")
                        query = f"{t.author} {clean_title}".strip()
                        _, s_uri = await search_spotify_api(query, self.http_session)
                        if s_uri:
                            seed_tracks.append(s_uri)

                if seed_tracks:
                    rec_result = await get_spotify_recommendation(seed_tracks, self.http_session)
                    if rec_result:
                        rec_name, rec_uri = rec_result if isinstance(rec_result, tuple) else (rec_result, None)
                        s_query = f"ytmsearch:{rec_name}"
                        tracks = await wavelink.Playable.search(s_query)
                        if tracks:
                            track = rank_best_track(tracks, s_query)
                            track.spotify_uri = rec_uri or seed_tracks[-1]
                            await player.queue.put_wait(track)
                            await player.play(track)
                            await self._update_panel(player, track=track)
                            if hasattr(player, "home") and player.home:
                                await player.home.send(f"🤖 **Spotify Autoplay** queued: **{track.title}**")
                            return

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        """
        AUTO RECOVERY: When a track fails to play, automatically:
        1. Log the error
        2. Try to play the next track in queue
        3. If queue is empty and 24/7 mode, re-queue lofi
        """
        player: wavelink.Player = payload.player
        if not player:
            return

        guild_id = player.guild.id if player.guild else 0
        node_id = getattr(getattr(player, "node", None), "identifier", "Private-Node")
        logger.error(
            f"Track failed on {node_id}: {payload.exception}. "
            f"Auto-recovering..."
        )

        # Keep player.autoplay permanently disabled
        player.autoplay = wavelink.AutoPlayMode.disabled

        # Allow fallback search if stream was blocked by YouTube
        retryable = (
            "requires login" in str(payload.exception).lower()
            or "confirm you're not a bot" in str(payload.exception).lower()
            or "sign in" in str(payload.exception).lower()
            or "no supported audio streams" in str(payload.exception).lower()
            or "all clients failed" in str(payload.exception).lower()
            or "400" in str(payload.exception).lower()
        )

        if retryable and payload.track and not getattr(payload.track, "_is_retry", False):
            failed_title = getattr(payload.track, "title", None)
            failed_author = getattr(payload.track, "author", None)
            if failed_title:
                clean_title = re.sub(r"\(.*?\)|\[.*?\]", "", failed_title).strip()
                queries = []
                if failed_author:
                    queries.append(f"ytmsearch:{failed_author} {clean_title}")
                queries.append(f"ytmsearch:{clean_title}")
                queries.append(clean_title)

                for fb_q in queries:
                    try:
                        logger.info(f"Retrying blocked track via fallback: {fb_q}")
                        fb_res = await asyncio.wait_for(wavelink.Playable.search(fb_q), timeout=6.0)
                        if fb_res:
                            fb_tracks = fb_res if isinstance(fb_res, list) else (fb_res.tracks if hasattr(fb_res, 'tracks') else [fb_res])
                            if fb_tracks:
                                fb_track = fb_tracks[0]
                                fb_track._title_override = payload.track.title
                                fb_track._is_retry = True
                                logger.info(f"Fallback matched: {payload.track.title} -> {fb_track.title}")
                                # We do NOT reset the consecutive fails counter here.
                                # It should only reset on successful completion of a track to prevent infinite loops!
                                self._consecutive_fails[guild_id] = 0
                                player.autoplay = wavelink.AutoPlayMode.disabled
                                await player.play(fb_track)
                                return
                    except Exception as fb_ex:
                        logger.warning(f"Fallback failed for '{fb_q}': {fb_ex}")

        player.autoplay = wavelink.AutoPlayMode.disabled

        if not hasattr(self, "_consecutive_fails"):
            self._consecutive_fails = {}
        self._consecutive_fails[guild_id] = self._consecutive_fails.get(guild_id, 0) + 1

        if self._consecutive_fails[guild_id] >= 3:
            logger.warning(f"3 consecutive tracks failed for guild {guild_id}. Halting auto-skip cascade to preserve queue.")
            player.autoplay = wavelink.AutoPlayMode.disabled
            try:
                await player.stop()
            except Exception:
                pass
            if hasattr(player, "home") and player.home:
                try:
                    await player.home.send(
                        "⚠️ **Playback Paused:** YouTube blocked stream playback on the server (`Sign in to confirm you're not a bot`).\n"
                        "Auto-skipping was halted immediately to protect your remaining queue.\n"
                        "💡 **Fix:** Please check the `BOBO NEW` console tab and authorize device at https://www.google.com/device.",
                        delete_after=45
                    )
                except Exception:
                    pass
            return

        # Advance exactly ONE track if fallback was not found
        if not player.queue.is_empty:
            next_track = player.queue.get()
            try:
                await player.play(next_track)
            except Exception:
                pass
        else:
            await player.stop()

        # Note: If player.autoplay is partial, Wavelink advances queue natively on track end.
        # Avoid manually popping player.queue.get() here to prevent double-skipping tracks!
        if player.guild and player.guild.id in self._247_guilds and player.queue.is_empty:
            # 24/7 mode: re-queue direct MP3 audio stream (bypasses YouTube live stream errors)
            logger.info("Track failed in 24/7 mode: re-queuing direct audio stream.")
            try:
                tracks: wavelink.Search = await wavelink.Playable.search("https://stream.zeno.fm/f3wvbbqmdg8uv")
                if tracks:
                    track = tracks[0] if isinstance(tracks, list) else (tracks.tracks[0] if hasattr(tracks, 'tracks') and tracks.tracks else tracks)
                    await player.play(track)
            except Exception as e:
                logger.error(f"Failed to recover 24/7 mode: {e}")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        """
        A track got stuck (no audio data received within trackStuckThresholdMs,
        configured as 10s in application.yml) — usually a symptom of network
        instability between Lavalink and the audio source. Auto-skip to the
        next track instead of leaving the bot silently hung on it.
        """
        player: wavelink.Player = payload.player
        if not player:
            return

        stuck_title = getattr(payload.track, "title", "the current track")
        logger.warning(f"Track stuck: '{stuck_title}' (threshold {payload.threshold}ms). Skipping to next track...")

        if hasattr(player, "home") and player.home:
            try:
                await player.home.send(f"⚠️ **{stuck_title}** got stuck buffering — skipping to the next track...", delete_after=8)
            except Exception:
                pass

        if not player.queue.is_empty:
            try:
                next_track = player.queue.get()
                player = await self._safe_play(player, next_track)
                logger.info(f"Recovered from stuck track: now playing {next_track.title}")
                if player.guild:
                    self._log_recovery_event(player.guild.id, "track_stuck_skip", success=True)
            except Exception as e:
                logger.error(f"Failed to recover from stuck track: {e}")
                if player.guild:
                    self._log_recovery_event(player.guild.id, "track_stuck_skip", success=False)
        else:
            try:
                await player.skip(force=True)
                if player.guild:
                    self._log_recovery_event(player.guild.id, "track_stuck_skip", success=True)
            except Exception as e:
                logger.error(f"Failed to skip stuck track with empty queue: {e}")
                if player.guild:
                    self._log_recovery_event(player.guild.id, "track_stuck_skip", success=False)

    @commands.Cog.listener()
    async def on_wavelink_node_disconnected(self, payload: wavelink.NodeDisconnectedEventPayload) -> None:
        """
        AUTO FAILOVER: fires when a previously-connected Lavalink node loses
        its connection. Note the correct wavelink event name is
        'node_disconnected' — NOT 'node_closed' (wavelink doesn't dispatch
        any event by that name; a previous version of this listener used
        the wrong name and silently never fired at all).

        The private node is first-priority always; a public backup is only
        connected here as a genuine last resort, when NO node is left at all.
        """
        node = payload.node
        logger.warning(f"Wavelink Node disconnected: {node.identifier}. Checking failover...")

        # Find all players that were on the disconnected node
        disconnected = [
            p for p in self.bot.voice_clients
            if isinstance(p, wavelink.Player) and getattr(p, "node", None) is not None
            and p.node.identifier == node.identifier
        ]

        for player in disconnected:
            if hasattr(player, "home") and player.home:
                try:
                    await player.home.send(
                        "⚠️ **Lavalink server disconnected.** Music playback is paused.",
                        delete_after=10
                    )
                except Exception:
                    pass

        # Check for remaining connected nodes
        remaining_connected = [
            n for n in wavelink.Pool.nodes.values() if n.status == wavelink.NodeStatus.CONNECTED
        ]
        if not remaining_connected:
            backups = getattr(self, "_public_backup_nodes", [])
            if not backups:
                logger.critical("❌ Lavalink node down and no public backups configured — music is fully offline until it reconnects.")
                for player in disconnected:
                    if hasattr(player, "home") and player.home:
                        try:
                            await player.home.send(
                                "❌ **Lavalink server is currently unreachable.** Music is temporarily offline. "
                                "We'll reconnect automatically when the server comes back online.",
                                delete_after=30
                            )
                        except Exception:
                            pass
                return

            logger.warning(f"⚠️ All nodes down! Cycling through {len(backups)} public backup nodes instantly...")
            already_connected_ids = {n.identifier for n in wavelink.Pool.nodes.values()}
            connected_backup = None

            for backup in backups:
                # Skip if this backup is already healthy
                if backup.identifier in already_connected_ids:
                    existing = wavelink.Pool.nodes.get(backup.identifier)
                    if existing and existing.status == wavelink.NodeStatus.CONNECTED:
                        connected_backup = backup
                        logger.info(f"✅ Reusing already-connected backup: {backup.identifier}")
                        break
                    continue

                logger.warning(f"🔄 Trying emergency backup: {backup.identifier} ({backup.uri})...")
                try:
                    await asyncio.wait_for(
                        wavelink.Pool.connect(nodes=[backup], client=self.bot, cache_capacity=100),
                        timeout=8.0  # Give each node 8 seconds max — instant failover
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"⚠️ Backup {backup.identifier} timed out (8s). Skipping to next...")
                    # Clean up the stuck node
                    stuck = wavelink.Pool.nodes.get(backup.identifier)
                    if stuck:
                        try:
                            await stuck.close()
                        except Exception:
                            pass
                    continue
                except Exception as e:
                    logger.warning(f"⚠️ Backup {backup.identifier} failed: {e}. Skipping to next...")
                    continue

                # Verify the node actually reached CONNECTED status
                await asyncio.sleep(2)
                node_now = wavelink.Pool.nodes.get(backup.identifier)
                if node_now and node_now.status == wavelink.NodeStatus.CONNECTED:
                    logger.info(f"✅ Emergency backup CONNECTED: {backup.identifier} ({backup.uri})")
                    connected_backup = backup
                    break
                else:
                    current_status = node_now.status if node_now else "not registered"
                    logger.warning(f"⚠️ Backup {backup.identifier} not CONNECTED (status: {current_status}). Trying next...")
                    if node_now is not None:
                        try:
                            await node_now.close()
                        except Exception:
                            pass
                    continue

            if connected_backup:
                for player in disconnected:
                    if hasattr(player, "home") and player.home:
                        try:
                            await player.home.send(
                                f"⚠️ **Private server is temporarily down — switched to public backup `{connected_backup.identifier}`** "
                                f"automatically. Music continues! We'll switch back to the private server once it's back online.",
                                delete_after=20
                            )
                        except Exception:
                            pass
            else:
                logger.critical("❌ ALL 10 public backups failed — bot has no working Lavalink node. Music is offline.")
                for player in disconnected:
                    if hasattr(player, "home") and player.home:
                        try:
                            await player.home.send(
                                "❌ **All Lavalink servers are currently unreachable.** Music is temporarily offline. "
                                "We'll reconnect automatically when a server comes back online.",
                                delete_after=30
                            )
                        except Exception:
                            pass


    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        node = payload.node
        logger.info(f"Wavelink Node connected: {node!r} | Resumed: {payload.resumed}")

        # ⭐ PRIVATE NODE PRIORITY:
        # When OptikLink private node comes back online,
        # move ALL active players from public nodes back to private instantly!
        if node.identifier == PRIMARY_NODE_ID:
            logger.info("🔄 Private primary node is back! Moving all players to primary node...")
            import asyncio

            async def _move_to_private():
                await asyncio.sleep(2)  # Give node 2s to fully initialize

                # Disconnect public backups FIRST — before reconnecting any
                # players. channel.connect() doesn't take an explicit node
                # to target; it connects via whichever node is available in
                # the Pool. Making the private node the ONLY one connected
                # guarantees the reconnect below actually lands there.
                for backup in getattr(self, "_public_backup_nodes", []):
                    backup_node = wavelink.Pool.nodes.get(backup.identifier)
                    if backup_node is not None:
                        try:
                            await backup_node.close()
                            logger.info(f"🔌 Disconnected emergency public backup ({backup.identifier}) before moving players back.")
                        except Exception as e:
                            logger.warning(f"Failed to disconnect public backup node {backup.identifier}: {e}")

                moved = 0
                try:
                    for player in list(self.bot.voice_clients):
                        if not isinstance(player, wavelink.Player):
                            continue
                        if getattr(player, "node", None) == node:
                            continue  # already on the private node
                        if not player.guild:
                            continue

                        guild_id = player.guild.id
                        if guild_id not in self._recovery_locks:
                            self._recovery_locks[guild_id] = asyncio.Lock()

                        try:
                            # Reuses the same disconnect → reconnect →
                            # restore-state logic already used for stale-
                            # session recovery — NOT player.move_to(), which
                            # is for moving between VOICE CHANNELS, not
                            # Lavalink nodes. Passing a Node object to
                            # move_to() there was the actual bug that made
                            # the bot leave voice when the private node
                            # came back.
                            await self._recover_stale_player(player, guild_id)
                            moved += 1
                            if hasattr(player, "home") and player.home:
                                try:
                                    await player.home.send(
                                        "⭐ **Private server is back online! Switched back for best quality!**",
                                        delete_after=6
                                    )
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.warning(f"Could not move player to private: {e}")
                    if moved > 0:
                        logger.info(f"✅ Moved {moved} player(s) back to {PRIMARY_NODE_ID}.")
                    else:
                        logger.info("ℹ️ No active players to move - private node ready for new sessions.")
                except Exception as e:
                    logger.warning(f"Failed to move players back to private node: {e}")

            asyncio.create_task(_move_to_private())


    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Detect disconnects AND Smart 24/7 Auto-Pause/Resume Power Saver Engine."""
        # 1. Bot disconnected/kicked from voice
        if member.id == self.bot.user.id and before.channel and not after.channel:
            guild_id = before.channel.guild.id
            if guild_id in self._recovering_guilds:
                return

            self._247_guilds.discard(guild_id)
            
            # Clean up the active UI panel
            panel_msg = self._active_panels.pop(guild_id, None)
            
            # Clean up tracking dicts
            self._recovery_locks.pop(guild_id, None)
            self._active_filters.pop(guild_id, None)
            self._active_filter_names.pop(guild_id, None)
            self._track_position.pop(guild_id, None)
            self._track_total.pop(guild_id, None)
            self._suppress_next_increment.discard(guild_id)
            if panel_msg:
                try:
                    await panel_msg.delete()
                except Exception:
                    pass
            return
            
        # 1.5 Bot moved to a new voice channel
        if member.id == self.bot.user.id and before.channel and after.channel and before.channel.id != after.channel.id:
            vc: wavelink.Player = member.guild.voice_client
            if vc:
                if hasattr(vc, "panel_message") and vc.panel_message:
                    try:
                        await vc.panel_message.delete()
                    except Exception:
                        pass
                
                embed = build_now_playing_embed(vc)
                view = MusicController(vc)
                try:
                    if vc.home:
                        msg = await vc.home.send(embed=embed, view=view)
                        vc.panel_message = msg
                        self._active_panels[member.guild.id] = msg
                except Exception:
                    pass


        # 2. Smart 24/7 Power-Saver Engine
        # Check only the guild where the voice state changed
        vc: wavelink.Player = member.guild.voice_client
        if vc and vc.channel:
            human_count = sum(1 for m in vc.channel.members if not m.bot)
            
            # If 0 humans in voice channel -> Auto-Pause (Save CPU & Bandwidth)
            if human_count == 0:
                if vc.playing and not vc.paused:
                    try:
                        await vc.pause(True)
                        logger.info(f"Smart Power Saver: Auto-paused playback in {vc.channel.name} (channel empty).")
                    except Exception:
                        pass
            
            # If human member joins channel while paused -> Auto-Resume
            elif human_count > 0 and vc.paused:
                try:
                    await vc.pause(False)
                    logger.info(f"Smart Power Saver: Auto-resumed playback in {vc.channel.name} (member joined).")
                except Exception:
                    pass


    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        """Send or edit Now Playing panel smoothly in-place for zero latency."""
        player: wavelink.Player = payload.player
        if not player or not player.guild:
            return

        track: wavelink.Playable = payload.track
        guild_id = player.guild.id

        if guild_id in self._suppress_next_increment:
            self._suppress_next_increment.discard(guild_id)
        elif guild_id in self._track_position:
            self._track_position[guild_id] += 1
            if self._track_position[guild_id] > self._track_total.get(guild_id, 0):
                self._track_total[guild_id] = self._track_position[guild_id]

        await self._update_panel(player, track=track)


    @commands.Cog.listener()
    async def on_wavelink_inactive(self, player: wavelink.Player) -> None:
        """
        CRITICAL: Override Wavelink's default inactive handler.
        Without this, Wavelink 3.4+ force-disconnects the bot after
        inactivity even if inactive_timeout is set to None.
        We intentionally do NOTHING here — the bot stays in the channel.
        """
        logger.info(f"Wavelink inactive event fired for guild {player.guild.id} — ignoring (24/7 mode active).")
        # Do NOT disconnect. Do NOT call player.disconnect().
        # This is intentional — the bot should stay in the channel.


    # ── Commands ──────────────────────────────────────────────────────────


    @commands.command(name="join", aliases=["connect", "summon"])
    async def join(self, ctx: commands.Context):
        channel = await _ensure_author_in_voice(ctx)
        if not channel:
            return
        
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            await _ensure_node_connected(self.bot)
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
        elif vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)

        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Disable auto-disconnect when idle

        # Auto-delete previous panel message if present
        if hasattr(vc, "panel_message") and vc.panel_message:
            try:
                await vc.panel_message.delete()
            except Exception:
                pass
            vc.panel_message = None

        view = MusicController(vc)        
        embed = build_now_playing_embed(vc)
        msg = await ctx.send(embed=embed, view=view)
        vc.panel_message = msg
        self._active_panels[ctx.guild.id] = msg

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    @commands.command(name="joinr", aliases=["jr", "radiojoin"])
    async def joinr(self, ctx: commands.Context):
        """Join voice channel and start 24/7 Lofi Radio immediately with UI Panel."""
        channel = await _ensure_author_in_voice(ctx, "❌ You need to be in a voice channel first.")
        if not channel:
            return

        # ⚡ Instant Feedback Message
        status_msg = await ctx.send("⏳ **Connecting to 24/7 Lofi Radio...**")

        vc: wavelink.Player = ctx.voice_client
        if not vc:
            await _ensure_node_connected(self.bot)
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await status_msg.edit(content="❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await status_msg.edit(content=f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await status_msg.edit(content=f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
        elif vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)

        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Disable auto-disconnect when idle
        self._247_guilds.add(ctx.guild.id)

        # ⚠️ PRIORITY ORDER MATTERS:
        # Zeno.fm FIRST — True infinite MP3 stream. No HLS auth token expiry. Never stops.
        # X4VbdwhkE10 SECOND — YouTube live stream works but HLS token expires every ~2-3 min.
        # ytsearch LAST — Finite video fallback.
        LOFI_SOURCES = [
            "https://stream.zeno.fm/f3wvbbqmdg8uv",           # ✅ True Infinite MP3 — BEST
            "https://www.youtube.com/watch?v=X4VbdwhkE10",   # YouTube live (HLS token expires)
            "ytsearch:lofi girl chill beats study",             # Last resort search
        ]

        track = None
        used_source = None
        for source in LOFI_SOURCES:
            try:
                results = await wavelink.Playable.search(source)
                if results:
                    track = results[0] if isinstance(results, list) else (results.tracks[0] if hasattr(results, 'tracks') else results)
                    used_source = source
                    break
            except Exception:
                continue

        try:
            if track:
                vc.queue.clear()
                if vc.playing or vc.paused:
                    await vc.skip(force=True)
                if vc.guild:
                    self._track_position.pop(vc.guild.id, None)
                    self._track_total.pop(vc.guild.id, None)
                try:
                    vc = await self._safe_play(vc, track)
                except Exception as e:
                    logger.error(f"Playback failed: {e}")
                    await ctx.send(f"❌ **Playback couldn't recover:** {e}")
                await status_msg.edit(content="☕ Joined & Started **24/7 Lofi Radio**!")
            else:
                await status_msg.edit(content="❌ Could not load Lofi Radio stream. Please try again.")
        except Exception as e:
            await status_msg.edit(content=f"❌ Radio Error: {str(e)}")

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass


    @commands.command(name="testspotify", aliases=["tsp"])
    async def test_spotify(self, ctx: commands.Context):
        """Test Spotify API connection and credentials."""
        msg = await ctx.send("🔍 Testing Spotify API...")
        
        try:
            session = self.http_session
            token = await _get_spotify_token(session)
            
            if not token:
                await msg.edit(content="❌ **Spotify API FAILED!**\n"
                                      f"• Client ID: `{str(SPOTIFY_CLIENT_ID)[:20] if SPOTIFY_CLIENT_ID else 'NOT SET'}...`\n"
                                      f"• Secret set: `{'Yes' if SPOTIFY_CLIENT_SECRET else 'No'}`\n"
                                      "Check your `.env` file!")
                return
            
            # Test API with a known playlist
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(
                "https://api.spotify.com/v1/playlists/37i9dQZF1DX0XUsuxWHRQd",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await msg.edit(content=f"✅ **Spotify API Working!**\n"
                                          f"• Token: Valid\n"
                                          f"• Test playlist: `{data.get('name', 'Unknown')}`\n"
                                          f"• Tracks: `{data.get('tracks', {}).get('total', 0)}`")
                else:
                    await msg.edit(content=f"❌ **Spotify API Error!**\n"
                                          f"• HTTP Status: `{resp.status}`\n"
                                          f"• Token valid but API request failed")
        except Exception as e:
            await msg.edit(content=f"❌ **Error:** ```{str(e)}```")

    @commands.command(name="server", aliases=["node", "lava", "lavalink"])
    async def server_status(self, ctx: commands.Context):
        """Show which Lavalink server the bot is currently connected to."""
        try:
            all_nodes = wavelink.Pool.nodes
        except Exception:
            all_nodes = {}

        if not all_nodes:
            embed = discord.Embed(
                title="❌ No Lavalink Nodes Connected",
                description="**Music will not work!** Check your configuration:\n\n"
                            "1. Verify your private Lavalink server is running\n"
                            "2. Check `.env` file settings:\n"
                            "   • `LAVALINK_HOST`\n"
                            "   • `LAVALINK_PORT`\n"
                            "   • `LAVALINK_PASSWORD`\n"
                            "3. Restart the bot after fixing",
                color=0xff0000
            )
            return await ctx.send(embed=embed, delete_after=30)

        # Get current player's node if playing
        vc: wavelink.Player = ctx.voice_client
        current_node_id = None
        if vc and hasattr(vc, "node") and vc.node:
            current_node_id = vc.node.identifier

        embed = discord.Embed(
            title="🎵 Lavalink Server Status",
            description="Real-time status of all music servers",
            color=0x00ff88
        )

        node_info = []
        private_working = False
        
        for node_id, node in all_nodes.items():
            is_private = node_id == PRIMARY_NODE_ID
            is_current = node_id == current_node_id
            is_connected = str(node.status).endswith("CONNECTED")

            if is_private and is_connected:
                private_working = True

            if is_connected:
                status_icon = "🟢"
            else:
                status_icon = "🔴"

            priority = "⭐ #1 PRIVATE" if is_private else "🌐 PUBLIC"
            current_tag = " **← ACTIVE**" if is_current else ""
            player_count = len([p for p in self.bot.voice_clients if getattr(p, 'node', None) and p.node.identifier == node_id])

            node_info.append(
                f"{status_icon} **{node_id}**{current_tag}\n"
                f"　{priority} | Players: `{player_count}` | URI: `{node.uri}`"
            )

        embed.add_field(
            name="📡 All Nodes",
            value="\n\n".join(node_info) if node_info else "No nodes found",
            inline=False
        )

        # Current playing server summary
        if current_node_id:
            is_private = current_node_id == PRIMARY_NODE_ID
            if is_private:
                embed.add_field(
                    name="✅ Status: OPTIMAL",
                    value="🔒 **Using Private OptikLink Server**\nBest performance & quality!",
                    inline=False
                )
            else:
                embed.add_field(
                    name="⚠️ Status: BACKUP MODE",
                    value=f"🌐 **Using Public Backup: {current_node_id}**\n"
                          f"Private server offline - check `.env` configuration",
                    inline=False
                )
        elif not private_working:
            embed.add_field(
                name="⚠️ Private Lavalink Offline",
                value="Your private server is not connected!\n"
                      f"Check `.env` settings and restart bot.\n\n"
                      f"Current config:\n"
                      f"• Host: `{os.getenv('LAVALINK_HOST', 'NOT SET')}`\n"
                      f"• Port: `{os.getenv('LAVALINK_PORT', 'NOT SET')}`",
                inline=False
            )

        embed.set_footer(text="Auto-failover enabled • Use !server to refresh status")
        await ctx.send(embed=embed, delete_after=45)
        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    @commands.command(aliases=['p', 'paly'])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def play(self, ctx: commands.Context, *, query: str = None):
        if query is None:
            if ctx.message.attachments:
                for att in ctx.message.attachments:
                    if (att.content_type and att.content_type.startswith("audio/")) or att.filename.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac')):
                        return await self.play_attachment(ctx, att)
            if not query:
                return await ctx.send("❌ Please provide a song name, link, or attach an audio file.")

        channel = await _ensure_author_in_voice(ctx, "❌ You need to be in a voice channel first.")
        if not channel:
            return

        vc: wavelink.Player = ctx.voice_client
        
        # ── Ghost State Fix ──
        # If Discord dropped the connection but Wavelink still thinks it's connected,
        # it will skip connecting and play invisibly. We must force a reconnect.
        if vc and not getattr(vc, "channel", None):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None

        if not vc:
            await _ensure_node_connected(self.bot)
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        if hasattr(self, "_consecutive_fails") and ctx.guild.id in self._consecutive_fails:
            self._consecutive_fails[ctx.guild.id] = 0
        vc.inactive_timeout = None  # Prevent auto-disconnect

        query = query.strip()
        url_match = re.search(r'(https?://\S+)', query)
        if url_match and ("\n" in query or "\r" in query or " " in query):
            clean_url = url_match.group(1).strip()
            if any(dom in clean_url.lower() for dom in ["youtube.com", "youtu.be", "spotify.com", "soundcloud.com"]):
                query = clean_url

        # ── YouTube Music "Liked Music" Link (list=LM) ──────────────────────
        # Regular playlist links (list=PL...) are handled further below via
        # Lavalink directly and are NOT affected by this branch.
        if "list=lm" in query.lower() and "youtube" in query.lower():
            status_msg = await ctx.send("🎵 Fetching your Liked Music from YouTube Music...")

            try:
                liked_queries = await resolve_liked_music_queries()

                if not liked_queries:
                    return await status_msg.edit(
                        content="❌ **Could not load Liked Music.**\n"
                                "This needs YouTube Music authentication set up (`ytmusicapi`). Possible reasons:\n"
                                "• No auth file configured — run `ytmusicapi oauth` in the bot's working "
                                "directory and set `YTMUSIC_AUTH_FILE` in `.env` if you name it something other "
                                "than `oauth.json`\n"
                                "• Auth token expired — regenerate it the same way\n"
                                "• `ytmusicapi` package not installed (`pip install ytmusicapi`)"
                    )

                loaded_tracks = []
                was_playing = vc.playing or vc.paused
                if not was_playing:
                    vc.queue.clear()
                    if vc.guild:
                        self._track_position[vc.guild.id] = 1
                        self._track_total[vc.guild.id] = len(liked_queries)
                        self._suppress_next_increment.add(vc.guild.id)
                first_query = liked_queries[0]
                try:
                    res = await wavelink.Playable.search(first_query)
                    first_track = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                    if not was_playing:
                        vc = await self._safe_play(vc, first_track)
                    else:
                        await vc.queue.put_wait(first_track)
                    loaded_tracks.append(first_track)
                    await status_msg.edit(content=f"⚡ Playing **{sanitize_md(first_track.title)}** now — loading {len(liked_queries)} liked songs in the background...")
                except Exception as e:
                    logger.warning(f"Failed to load first liked-music track: {e}")

                async def background_loader(_vc=vc, _status=status_msg, _queries=liked_queries[1:], _loaded=loaded_tracks):
                    semaphore = asyncio.Semaphore(5)
                    progress_lock = asyncio.Lock()
                    
                    async def _load_one(q: str):
                        async with semaphore:
                            if not getattr(_vc, "channel", None): return
                            try:
                                res = await asyncio.wait_for(wavelink.Playable.search(q), timeout=20.0)
                                t = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                                if t:
                                    async with progress_lock:
                                        await _vc.queue.put_wait(t)
                                        _loaded.append(t)
                            except Exception as ex:
                                logger.warning(f"Liked Music loader exception for {q}: {ex}")
                            await asyncio.sleep(0.3)
                            
                    await asyncio.gather(*[_load_one(q) for q in _queries])
                    try:
                        await _status.edit(content=f"✅ Finished loading **{len(_loaded)}** liked songs!", delete_after=15)
                    except Exception:
                        pass

                asyncio.create_task(background_loader())

                try:
                    await ctx.message.delete(delay=3)
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"Liked Music playlist error: {e}", exc_info=True)
                await status_msg.edit(content=f"❌ Unexpected error loading Liked Music: {str(e)}")
            return

        # ── Spotify Link ─────────────────────────────────────────────────────
        if "spotify.com" in query or "open.spotify.com" in query:
            status_msg = await ctx.send("🎵 Fetching Spotify tracks...")
            
            try:
                spotify_queries = await resolve_spotify_query(query, session=self.http_session)
                
                if not spotify_queries or len(spotify_queries) == 0:
                    await status_msg.edit(content="❌ **Spotify Error:** Could not fetch tracks from this playlist.\n"
                                                  "Possible reasons:\n"
                                                  "• Playlist is private or region-locked\n"
                                                  "• Spotify API timeout (try again)\n"
                                                  "• Invalid playlist URL")
                    return

                # Check if it's just a fallback query (means API failed)
                first_item = spotify_queries[0] if spotify_queries else {}
                first_q_str = first_item["query"] if isinstance(first_item, dict) else str(first_item)
                if len(spotify_queries) == 1 and ("ytmsearch:spotify" in first_q_str or "ytsearch:spotify" in first_q_str):
                    await status_msg.edit(content="❌ **Spotify API failed!** Credentials invalid or timeout.\n"
                                                  "Try using direct links instead: `!play <song name>`")
                    return

                was_playing = vc.playing or vc.paused
                loaded_tracks = []
                if not was_playing and vc.guild:
                    self._track_position[vc.guild.id] = 1
                    self._track_total[vc.guild.id] = len(spotify_queries)
                    self._suppress_next_increment.add(vc.guild.id)

                # --- 🚀 Instant Playback Optimization ---
                # Load the very first track immediately so music starts playing without waiting
                try:
                    from wavelink import TrackSource
                    yt_src = TrackSource.YouTube
                    sc_src = TrackSource.SoundCloud
                except Exception:
                    yt_src = "ytsearch"
                    sc_src = "scsearch"

                try:
                    if isinstance(first_item, dict):
                        clean_q = first_item["query"].replace("ytsearch:", "").replace("ytmsearch:", "").strip()
                        dur_ms = first_item.get("duration_ms")
                        art = first_item.get("artist")
                    else:
                        clean_q = str(first_item).replace("ytsearch:", "").replace("ytmsearch:", "").strip()
                        dur_ms = None
                        art = None

                    try:
                        result = await asyncio.wait_for(wavelink.Playable.search(clean_q, source=sc_src), timeout=20.0)
                    except Exception:
                        result = None
                        
                    if not result:
                        logger.info("SoundCloud empty or timed out, trying YouTube fallback for first track")
                        try:
                            result = await asyncio.wait_for(wavelink.Playable.search(clean_q, source=yt_src), timeout=20.0)
                        except Exception:
                            result = None
                        
                    if result:
                        track = pick_best_spotify_match(result, dur_ms, art)
                        if not track:
                            track = result[0] if isinstance(result, list) else (result.tracks[0] if hasattr(result, 'tracks') else result)
                        await vc.queue.put_wait(track)
                        loaded_tracks.append(track)
                        
                        if not was_playing:
                            next_track = vc.queue.get()
                            try:
                                vc = await self._safe_play(vc, next_track)
                            except Exception as e:
                                logger.error(f"Playback failed: {e}")
                                await ctx.send(f"❌ **Playback couldn't recover:** {e}")
                            await ctx.send(f"⚡ Playing **{sanitize_md(track.title)}** now while the rest of the playlist loads...", delete_after=10)
                except Exception as e:
                    logger.warning(f"Failed to load first track ({first_item}): {e}")

                # If there are more tracks, load them in the background so we don't freeze the bot
                if len(spotify_queries) > 1:
                    async def background_loader(_vc=vc, _status=status_msg, _queries=spotify_queries[1:], _loaded=loaded_tracks, _yt=yt_src, _sc=sc_src):
                        total = len(_queries) + 1
                        # Load up to 5 tracks concurrently instead of one at a time —
                        # this is roughly 5x faster for large playlists while still
                        # staying gentle on the Lavalink node (each lookup still has
                        # its own timeout, and a small per-task delay avoids bursting
                        # all requests at once).
                        semaphore = asyncio.Semaphore(5)
                        progress_lock = asyncio.Lock()

                        async def _load_one(item: dict | str):
                            async with semaphore:
                                if not _player_valid(_vc):
                                    return
                                try:
                                    if isinstance(item, dict):
                                        sq = item["query"]
                                        dur_ms = item.get("duration_ms")
                                        art = item.get("artist")
                                    else:
                                        sq = str(item)
                                        dur_ms = None
                                        art = None

                                    clean_q = sq.replace("ytsearch:", "").replace("ytmsearch:", "").strip()
                                    try:
                                        res = await asyncio.wait_for(wavelink.Playable.search(clean_q, source=_yt), timeout=20.0)
                                    except Exception:
                                        res = None

                                    if not res:
                                        try:
                                            res = await asyncio.wait_for(wavelink.Playable.search(clean_q, source=_sc), timeout=20.0)
                                        except Exception:
                                            res = None

                                    if res:
                                        t = pick_best_spotify_match(res, dur_ms, art)
                                        if not t:
                                            t = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, 'tracks') else res)
                                        await _vc.queue.put_wait(t)
                                        async with progress_lock:
                                            _loaded.append(t)
                                            if len(_loaded) % 5 == 0 or len(_loaded) == total - 1:
                                                try:
                                                    await _status.edit(content=f"🎵 Loading Spotify tracks in background... ({len(_loaded)}/{total})")
                                                except Exception:
                                                    pass
                                    else:
                                        logger.warning(f"Spotify Loader: wavelink returned empty for {item}")
                                except Exception as ex:
                                    logger.error(f"Spotify Loader exception for {item}: {ex}")

                                # Small stagger even within the semaphore slot so we
                                # don't hammer Lavalink/YouTube the instant a slot frees up.
                                await asyncio.sleep(0.3)

                        await asyncio.gather(*[_load_one(item) for item in _queries])
                        
                        try:
                            msg = f"✅ Finished loading **{len(_loaded)}** tracks from Spotify!"
                            await _status.edit(content=msg, delete_after=15)
                        except Exception:
                            pass
                            
                    # Start background loading task
                    asyncio.create_task(background_loader())
                else:
                    await status_msg.edit(content=f"✅ Loaded Spotify track!", delete_after=5)

                try:
                    await ctx.message.delete(delay=3)
                except Exception:
                    pass
                    
            except Exception as e:
                logger.error(f"Spotify playlist error: {e}", exc_info=True)
                await status_msg.edit(content=f"❌ Unexpected error: {str(e)}\nTry using direct YouTube links instead.")
            return

        # ── SoundCloud / YouTube / Direct URL ────────────────────────────────
        spotify_uri = None
        yt_playlist_id = None
        if ("youtube.com" in query.lower() or "youtu.be" in query.lower()) and "list=" in query:
            match = re.search(r"list=([a-zA-Z0-9_-]+)", query)
            if match:
                yt_playlist_id = match.group(1)
                if yt_playlist_id.lower() != "lm":
                    search_query = f"https://www.youtube.com/playlist?list={yt_playlist_id}"
                else:
                    yt_playlist_id = None
        
        if not yt_playlist_id:
            search_query = query if (query.startswith("http://") or query.startswith("https://") or ":" in query) else f"ytsearch:{query}"

        status_msg = None
        is_youtube_playlist = bool(yt_playlist_id)

        if is_youtube_playlist:
            status_msg = await ctx.send("⚡ **Loading playlist...**")

        try:
            tracks = await wavelink.Playable.search(search_query)
        except Exception:
            tracks = None

        if (not tracks or (isinstance(tracks, list) and len(tracks) == 0)) and yt_playlist_id:
            try:
                native_tracks = await _native_youtube_playlist_scrape(yt_playlist_id)
                if native_tracks:
                    first_item = native_tracks[0]
                    first_track = await _resolve_playable_track(first_item)
                    if first_track:
                        was_playing = vc.playing or vc.paused
                        if not was_playing:
                            vc = await self._safe_play(vc, first_track)
                            if vc.guild:
                                self._track_position[vc.guild.id] = 1
                                self._track_total[vc.guild.id] = len(native_tracks)
                                self._suppress_next_increment.add(vc.guild.id)
                        else:
                            await vc.queue.put_wait(first_track)

                        display_name = getattr(first_track, "_title_override", first_track.title)
                        if status_msg:
                            try:
                                await status_msg.edit(content=f"⚡ Playing **{sanitize_md(display_name)}** (`{len(native_tracks)}` tracks queued)", delete_after=5)
                            except Exception:
                                pass

                        async def bg_native_loader(_vc=vc, _items=native_tracks[1:]):
                            sem = asyncio.Semaphore(5)
                            async def _load_t(item):
                                async with sem:
                                    if not _player_valid(_vc): return
                                    try:
                                        t = await _resolve_playable_track(item)
                                        if t:
                                            await _vc.queue.put_wait(t)
                                    except Exception:
                                        pass
                                    await asyncio.sleep(0.05)
                            await asyncio.gather(*[_load_t(item) for item in _items])

                        asyncio.create_task(bg_native_loader())
                        return
            except Exception as e:
                logger.error(f"Native playlist fallback failed: {e}", exc_info=True)

        if (not tracks or (isinstance(tracks, list) and len(tracks) == 0)) and yt_playlist_id:
            if status_msg:
                try:
                    await status_msg.edit(
                        content="❌ **Could not load playlist!**\n"
                                "This YouTube playlist is **Private** or does not exist.\n"
                                "💡 **How to fix:** Open the playlist in YouTube, change privacy from **Private** to **Unlisted** or **Public**, then paste the link again!"
                    )
                except Exception:
                    pass
            return

        if not tracks and search_query.startswith("ytsearch:"):
            clean_query = search_query.replace("ytsearch:", "").strip()
            # Fallback 1: YouTube Music
            try:
                tracks = await wavelink.Playable.search(clean_query, source=wavelink.TrackSource.YouTubeMusic)
            except Exception:
                tracks = None

            if not tracks:
                # Fallback 1.5: Native Python Web Scrape Bypass
                native_url = await _native_youtube_search(query)
                if native_url:
                    try:
                        tracks = await wavelink.Playable.search(native_url)
                    except Exception:
                        tracks = None

            if not tracks:
                # Fallback: Native Python Web Scrape Bypass
                native_url = await _native_youtube_search(query)
                if native_url:
                    try:
                        tracks = await wavelink.Playable.search(native_url)
                    except Exception:
                        tracks = None

        if not tracks:
            if status_msg:
                return await status_msg.edit(content=f"❌ No results found for: **{query}**")
            return await ctx.send(f"❌ No results found for: **{query}**")

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        was_playing = vc.playing or vc.paused

        if isinstance(tracks, wavelink.Playlist):
            added = await vc.queue.put_wait(tracks)
            await ctx.send(f"✅ Added playlist **{sanitize_md(tracks.name)}** (`{added}` tracks) to the queue.", delete_after=5)
            if not was_playing and vc.guild:
                self._track_position[vc.guild.id] = 1
                self._track_total[vc.guild.id] = added
                self._suppress_next_increment.add(vc.guild.id)
        else:
            track = rank_best_track(tracks, search_query)
            if spotify_uri:
                track.spotify_uri = spotify_uri
            await vc.queue.put_wait(track)
            await ctx.send(f"✅ Added **{sanitize_md(track.title)}** to the queue.", delete_after=5)
            # A single non-playlist track — clear any stale position tracking
            # from a previous playlist so the Track field doesn't show a
            # leftover count that no longer applies.
            if not was_playing and vc.guild:
                self._track_position.pop(vc.guild.id, None)
                self._track_total.pop(vc.guild.id, None)


        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

        if not was_playing and not vc.playing:
            next_track = vc.queue.get()
            try:
                vc = await self._safe_play(vc, next_track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                await ctx.send(f"❌ **Playback couldn't recover:** {e}")


    @commands.command(name="playnow", aliases=["pn", "playinstant"])
    async def playnow(self, ctx: commands.Context, *, query: str):
        """
        Play a song IMMEDIATELY, skipping the current track.
        """
        channel = await _ensure_author_in_voice(ctx, "❌ You need to be in a voice channel first.")
        if not channel:
            return

        vc: wavelink.Player = ctx.voice_client
        if not vc:
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Disable auto-disconnect when idle

        query = query.strip()

        # ── Spotify Link ─────────────────────────────────────────────────────
        if "spotify.com" in query or "open.spotify.com" in query:
            try:
                spotify_queries = await resolve_spotify_query(query, session=self.http_session)
                first_item = spotify_queries[0] if spotify_queries else {}
                first_q_str = first_item["query"] if isinstance(first_item, dict) else str(first_item)
                if not spotify_queries or (len(spotify_queries) == 1 and ("ytmsearch:spotify" in first_q_str or "ytsearch:spotify" in first_q_str)):
                    return await ctx.send("❌ Spotify API failed. Try a direct link instead.", delete_after=5)

                if isinstance(first_item, dict):
                    search_str = first_item["query"]
                    dur_ms = first_item.get("duration_ms")
                    art = first_item.get("artist")
                else:
                    search_str = str(first_item)
                    dur_ms = None
                    art = None

                # playnow: only take the first track for instant playback
                result = await wavelink.Playable.search(search_str)
                if not result:
                    return await ctx.send(f"❌ Could not resolve Spotify track.", delete_after=5)
                track = pick_best_spotify_match(result, dur_ms, art)
                if not track:
                    track = result[0] if isinstance(result, list) else (result.tracks[0] if hasattr(result, 'tracks') else result)
            except Exception as e:
                return await ctx.send(f"❌ Spotify error: {str(e)}", delete_after=5)

            vc.queue.clear()
            if vc.playing or vc.paused:
                await vc.skip(force=True)
            if vc.guild:
                self._track_position.pop(vc.guild.id, None)
                self._track_total.pop(vc.guild.id, None)
            try:
                vc = await self._safe_play(vc, track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                await ctx.send(f"❌ **Playback couldn't recover:** {e}")
            await ctx.send(f"⚡ Playing **{sanitize_md(track.title)}** now!", delete_after=5)
            try:
                await ctx.message.delete(delay=3)
            except Exception:
                pass
            return

        # ── SoundCloud / YouTube / Direct URL ────────────────────────────────
        spotify_uri = None
        if "soundcloud.com" in query or query.startswith("ytmsearch:"):
            search_query = query
        else:
            search_query = query if (query.startswith("http://") or query.startswith("https://") or ":" in query) else f"ytsearch:{query}"
        status_msg = None
        if "playlist" in search_query.lower() and "youtube" in search_query.lower():
            status_msg = await ctx.send("⚡ **Loading playlist...**")

        try:
            tracks: wavelink.Search = await wavelink.Playable.search(search_query)
        except Exception:
            tracks = None

        if not tracks and search_query.startswith("ytsearch:"):
            clean_query = search_query.replace("ytsearch:", "").strip()
            # Fallback 1: YouTube Music
            try:
                tracks = await wavelink.Playable.search(clean_query, source=wavelink.TrackSource.YouTubeMusic)
            except Exception:
                tracks = None

            if not tracks:
                # Fallback: Native Python Web Scrape Bypass
                native_url = await _native_youtube_search(query)
                if native_url:
                    try:
                        tracks = await wavelink.Playable.search(native_url)
                    except Exception:
                        tracks = None

        if not tracks:
            # Fallback: Native Python Web Scrape Bypass
            native_url = await _native_youtube_search(query)
            if native_url:
                try:
                    tracks = await wavelink.Playable.search(native_url)
                except Exception:
                    tracks = None

        if not tracks:
            if status_msg:
                return await status_msg.edit(content=f"❌ No results found for `{query}`.")
            return await ctx.send(f"❌ No results found for `{query}`.")

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        track = rank_best_track(tracks, search_query)
        if spotify_uri:
            track.spotify_uri = spotify_uri
        vc.queue.clear()
        if vc.playing or vc.paused:
            await vc.skip(force=True)
        if vc.guild:
            self._track_position.pop(vc.guild.id, None)
            self._track_total.pop(vc.guild.id, None)
        try:
            vc = await self._safe_play(vc, track)
        except Exception as e:
            logger.error(f"Playback failed: {e}")
            await ctx.send(f"❌ **Playback couldn't recover:** {e}")
        await ctx.send(f"⚡ Playing **{sanitize_md(track.title)}** right now!", delete_after=5)


        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass


    @commands.command(name="24/7")
    async def lofi_247(self, ctx: commands.Context):
        channel = await _ensure_author_in_voice(ctx)
        if not channel:
            return
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
        vc.home = ctx.channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None  # Prevent auto-disconnect
        self._247_guilds.add(ctx.guild.id)

        try:
            tracks: wavelink.Search = await wavelink.Playable.search("ytmsearch:lofi girl live radio")
            if tracks:
                track = tracks[0]
                if vc.playing or vc.paused:
                    await vc.queue.put_wait(track)
                    await ctx.send("Lofi Radio queued!", delete_after=5)
                else:
                    if vc.guild:
                        self._track_position.pop(vc.guild.id, None)
                        self._track_total.pop(vc.guild.id, None)
                    try:
                        vc = await self._safe_play(vc, track)
                    except Exception as e:
                        logger.error(f"Playback failed: {e}")
                        await ctx.send(f"❌ **Playback couldn't recover:** {e}")
                    await ctx.send("Starting 24/7 Lofi Radio... Panel incoming!", delete_after=5)
            else:
                await ctx.send("Could not find a Lofi stream. Try `!play lofi girl radio`", delete_after=5)
        except Exception as e:
            await ctx.send(f"Error: {str(e)}", delete_after=5)

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    @commands.command(name="search", aliases=["find"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def search_cmd(self, ctx: commands.Context, *, query: str):
        """Search YouTube for top 5 results and let the user pick interactively."""
        channel = await _ensure_author_in_voice(ctx)
        if not channel:
            return

        search_query = query if (query.startswith("http://") or query.startswith("https://") or ":" in query) else f"ytsearch:{query}"

        try:
            tracks: wavelink.Search = await wavelink.Playable.search(search_query)
            if not tracks:
                return await ctx.send(f"No results found for `{query}`.", delete_after=5)
        except Exception as e:
            return await ctx.send(f"Error: {str(e)}", delete_after=5)

        tracks_list = list(tracks) if isinstance(tracks, (list, tuple)) else (list(tracks.tracks) if hasattr(tracks, 'tracks') else [tracks])
        top_5 = tracks_list[:5]

        embed = discord.Embed(
            title=f"🔍 Search Results for '{query}'",
            description="Select a song from the dropdown menu below to add it to the queue:",
            color=0x0284c7
        )

        for i, track in enumerate(top_5):
            dur_ms = getattr(track, "length", 0) or 0
            dur_sec = int(dur_ms / 1000)
            dur_str = f"{dur_sec // 60:02d}:{dur_sec % 60:02d}"
            clean_t = sanitize_md(getattr(track, "title", "Unknown Track"))
            clean_a = sanitize_md(getattr(track, "author", "Unknown Artist"))
            embed.add_field(
                name=f"{i+1}. {clean_t}",
                value=f"By **`{clean_a}`** • Duration: `{dur_str}`",
                inline=False
            )

        view = SearchView(top_5, ctx)
        await ctx.send(embed=embed, view=view, delete_after=30)

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    @commands.command(name="panel")
    async def panel(self, ctx: commands.Context):

        vc: wavelink.Player = ctx.voice_client
        if not vc:
            return await ctx.send("I am not playing anything right now.", delete_after=5)

        # Auto-delete previous panel message if present
        if hasattr(vc, "panel_message") and vc.panel_message:
            try:
                await vc.panel_message.delete()
            except Exception:
                pass
            vc.panel_message = None

        view = MusicController(vc)
        embed = build_now_playing_embed(vc)
        vc.panel_message = await ctx.send(embed=embed, view=view)

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass


    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            return await ctx.send("I am not in a voice channel.", delete_after=5)

        # Save session before disconnecting
        await self.save_session_for_guild(ctx.guild.id, vc)

        # Delete panel message on stop
        if hasattr(vc, "panel_message") and vc.panel_message:
            try:
                await vc.panel_message.delete()
            except Exception:
                pass
            vc.panel_message = None

        self._247_guilds.discard(ctx.guild.id)
        self._recovery_locks.pop(ctx.guild.id, None)
        self._active_filters.pop(ctx.guild.id, None)
        self._active_filter_names.pop(ctx.guild.id, None)
        self._track_position.pop(ctx.guild.id, None)
        self._track_total.pop(ctx.guild.id, None)
        self._suppress_next_increment.discard(ctx.guild.id)
        await vc.disconnect()
        await ctx.send("Stopped and left the voice channel.", delete_after=5)

        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    async def save_session_for_guild(self, guild_id: int, player: wavelink.Player | None = None):
        """Save exactly one last active session for the guild to the DB before clean disconnect."""
        try:
            if not player:
                guild = self.bot.get_guild(guild_id)
                player = getattr(guild, "voice_client", None) if guild else None

            if not player or not getattr(player, "channel", None):
                return

            voice_channel_id = player.channel.id
            current_track = getattr(player, "current", None)
            current_track_uri = current_track.uri if (current_track and getattr(current_track, "uri", None)) else ""
            position_ms = int(getattr(player, "position", 0))
            volume = int(getattr(player, "volume", 100))
            filter_name = self._active_filter_names.get(guild_id, "Normal")
            track_pos = self._track_position.get(guild_id, 0)
            track_tot = self._track_total.get(guild_id, 0)

            queue_uris = []
            if hasattr(player, "queue") and player.queue:
                for t in list(player.queue):
                    uri = getattr(t, "uri", None)
                    if uri:
                        queue_uris.append(uri)

            # Only overwrite if there was actually a track or queue in this session
            if not current_track_uri and not queue_uris:
                return

            await db._run(
                db.save_last_session,
                guild_id,
                voice_channel_id,
                current_track_uri,
                position_ms,
                volume,
                filter_name,
                track_pos,
                track_tot,
                queue_uris
            )
            logger.info(f"Saved last playlist session for guild {guild_id} (track: '{getattr(current_track, 'title', '')}', queue: {len(queue_uris)})")
        except Exception as e:
            logger.error(f"Error saving session for guild {guild_id}: {e}", exc_info=True)

    async def _apply_named_filter(self, player: wavelink.Player, filter_name: str):
        """Applies a filter preset by name to the given player."""
        try:
            filters = wavelink.Filters()
            guild_id = player.guild.id
            val = filter_name.lower().strip()

            if val in ("clear", "normal", ""):
                self._active_filters.pop(guild_id, None)
                self._active_filter_names.pop(guild_id, None)
                await player.set_filters(filters)
                return

            if val == "bassboost":
                filters.equalizer.set(band=0, gain=0.60)
                filters.equalizer.set(band=1, gain=0.50)
                filters.equalizer.set(band=2, gain=0.35)
                filters.equalizer.set(band=3, gain=0.20)
                filters.equalizer.set(band=4, gain=0.10)
            elif val == "lofi":
                filters.timescale.set(speed=0.95, pitch=0.95, rate=1.0)
                filters.low_pass.set(smoothing=15.0)
                filters.equalizer.set(band=0, gain=0.25)
                filters.equalizer.set(band=1, gain=0.15)
            elif val == "nightcore":
                filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
            elif val == "vaporwave":
                filters.timescale.set(speed=0.8, pitch=0.8, rate=1.0)
                filters.low_pass.set(smoothing=12.0)
            elif val == "8d":
                filters.rotation.set(rotation_hz=0.25)
            elif val == "karaoke":
                filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
                for b in range(4, 9):
                    filters.equalizer.set(band=b, gain=-0.25)

            self._active_filters[guild_id] = filters
            self._active_filter_names[guild_id] = val
            await player.set_filters(filters)
        except Exception as e:
            logger.debug(f"Error applying named filter {filter_name}: {e}")

    async def restore_session_for_guild(
        self,
        guild: discord.Guild,
        target_channel: discord.VoiceChannel | discord.StageChannel,
        text_channel: discord.TextChannel | None = None
    ) -> tuple[bool, str, wavelink.Player | None]:
        """
        Shared underlying restore implementation used by both !resume command
        and web dashboard /api/player/{guild_id}/resume endpoint.
        """
        session = await db._run(db.get_last_session, guild.id)
        if not session or (not session.get("current_track_uri") and not session.get("queue_uris")):
            return False, "No previous session to resume.", None

        # 1. Connect or Move Player to target_channel
        vc: wavelink.Player = guild.voice_client
        if vc and not getattr(vc, "channel", None):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None

        if not vc:
            await _ensure_node_connected(self.bot)
            try:
                vc = await _connect_voice_with_retry(target_channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return False, "The Lavalink Music Server is currently offline or restarting.", None
            except VoiceChannelPermissionError as e:
                return False, f"Missing voice channel permissions: {e}", None
            except Exception as e:
                return False, f"Voice connection error: {e}", None
        elif vc.channel.id != target_channel.id:
            try:
                await vc.move_to(target_channel)
            except Exception as e:
                return False, f"Could not move to voice channel: {e}", None

        if text_channel:
            vc.home = text_channel
        vc.autoplay = wavelink.AutoPlayMode.disabled
        vc.inactive_timeout = None

        # 2. Reset tracking and suppress increment on start
        self._track_position[guild.id] = session.get("track_position", 1) or 1
        self._track_total[guild.id] = session.get("track_total", 1) or 1
        self._suppress_next_increment.add(guild.id)

        # 3. Clear existing queue and resolve saved tracks
        vc.queue.clear()

        cur_uri = session.get("current_track_uri")
        cur_track = None
        if cur_uri:
            try:
                res = await wavelink.Playable.search(cur_uri)
                if res:
                    cur_track = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, "tracks") else res)
            except Exception as e:
                logger.warning(f"Failed to resolve current track uri {cur_uri}: {e}")

        q_uris = session.get("queue_uris", [])
        for uri in q_uris:
            try:
                res = await wavelink.Playable.search(uri)
                if res:
                    track = res[0] if isinstance(res, list) else (res.tracks[0] if hasattr(res, "tracks") else res)
                    await vc.queue.put_wait(track)
            except Exception:
                pass

        # 4. Set volume
        saved_vol = session.get("volume", 100)
        try:
            await vc.set_volume(saved_vol)
        except Exception:
            pass

        # 5. Set filter
        filter_name = session.get("filter_name", "Normal")
        if filter_name and filter_name not in ("Normal", "clear"):
            await self._apply_named_filter(vc, filter_name)

        # 6. Play and seek
        pos_ms = session.get("position_ms", 0)
        track_to_play = cur_track
        if not track_to_play and not vc.queue.is_empty:
            track_to_play = vc.queue.get()

        if track_to_play:
            await self._safe_play(vc, track_to_play)
            if pos_ms > 1000:
                async def _delayed_seek(p: wavelink.Player, t, pos):
                    await asyncio.sleep(0.8)
                    try:
                        if p.playing and p.current == t:
                            await p.seek(pos)
                    except Exception as e:
                        logger.debug(f"Delayed resume seek error: {e}")
                asyncio.create_task(_delayed_seek(vc, track_to_play, pos_ms))

        # 7. Send or update Now Playing panel
        if text_channel:
            try:
                embed = build_now_playing_embed(vc)
                view = MusicController(vc)
                msg = await text_channel.send(embed=embed, view=view)
                vc.panel_message = msg
                self._active_panels[guild.id] = msg
            except Exception as e:
                logger.debug(f"Could not send Now Playing panel on resume: {e}")

        # 8. Broadcast state to web dashboard
        api_cog = self.bot.get_cog("APICog")
        if api_cog and hasattr(api_cog, "broadcast_state"):
            asyncio.create_task(api_cog.broadcast_state(guild.id))

        return True, "Session restored successfully.", vc

    @commands.command(name="resume", aliases=["resumelast", "restorelast"])
    async def resume_last(self, ctx: commands.Context):
        """Resume the previous playlist session in your current voice channel."""
        vc: wavelink.Player = ctx.voice_client
        # If bot is currently in channel and paused, standard resume unpauses
        if vc and _player_valid(vc) and vc.paused and vc.current:
            await vc.pause(False)
            return await ctx.send("▶ **Resumed playback.**", delete_after=5)

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("❌ You need to be in a voice channel to resume a session.", delete_after=5)

        target_channel = ctx.author.voice.channel
        status_msg = await ctx.send("⏳ **Restoring previous session...**")
        success, msg, player = await self.restore_session_for_guild(ctx.guild, target_channel, ctx.channel)
        if success:
            try:
                await status_msg.edit(content=f"▶ **Resumed previous session in {target_channel.name}!**")
            except Exception:
                pass
        else:
            try:
                await status_msg.edit(content=f"❌ {msg}")
            except Exception:
                pass

    async def _execute_skip(self, player: wavelink.Player) -> tuple[bool, str]:
        """
        Unified skip implementation used by both !skip command and Discord UI skip button.
        Handles playing, paused, and stopped-with-queue states perfectly for 1100+ song playlists.
        """
        if not _player_valid(player):
            return False, "Not connected to a voice channel."

        # If both current track is empty AND queue is empty, there is nothing to skip
        if not player.current and not player.playing and not player.paused and player.queue.is_empty:
            return False, "Queue is empty. Nothing to play or skip."

        now = time.time()
        if getattr(player, "_skip_lock", False) or (now - getattr(player, "_last_skip_time", 0) < 1.0):
            return False, "⏱️ Please wait a moment before skipping again."
        player._skip_lock = True
        player._last_skip_time = now

        try:
            # Handle radio session restore
            if getattr(player.current, "_is_radio", False) and getattr(player, "_saved_playlist_session", None):
                api_cog = self.bot.get_cog("APICog") or self.bot.get_cog("api")
                if api_cog and hasattr(api_cog, "restore_saved_playlist"):
                    await api_cog.restore_saved_playlist(player)
                    return True, "📻 Radio stopped — Resumed paused playlist from stop location! 📻"

            # Push current track to history manually (since reason=replaced bypasses it)
            if player.current and hasattr(player.queue, "history"):
                try:
                    player.queue.history.put(player.current)
                except Exception:
                    pass

            # If there are tracks in queue: pop the next one and play it directly
            if not player.queue.is_empty:
                next_track = player.queue.get()
                display_name = getattr(next_track, "_title_override", next_track.title)

                if player.guild:
                    guild_id = player.guild.id
                    if guild_id in self._track_position:
                        self._track_position[guild_id] += 1
                        if self._track_position[guild_id] > self._track_total.get(guild_id, 0):
                            self._track_total[guild_id] = self._track_position[guild_id]
                    self._suppress_next_increment.add(guild_id)

                await player.play(next_track)
                await self._update_panel(player, track=next_track)
                return True, f"⏭️ Skipped to **{display_name}**"

            # If queue is empty but a track is playing, just stop it
            if player.current:
                await player.stop()
                await self._update_panel(player, track=None)
                return True, "⏭️ Skipped. Queue is now empty."

            return False, "Queue is empty."
        except Exception as e:
            import logging
            logging.getLogger("nexus.music").error(f"Skip error: {e}")
            return False, f"❌ Failed to skip: {e}"
        finally:
            player._skip_lock = False

    async def _execute_previous(self, player: wavelink.Player) -> tuple[bool, str]:
        """
        Unified backskip (previous track) implementation used by both !previous/!back
        commands and Discord UI ⏮️ button. Works seamlessly with 1100+ song playlists.
        """
        if not _player_valid(player):
            return False, "Not connected to a voice channel."

        now = time.time()
        if getattr(player, "_prev_lock", False) or (now - getattr(player, "_last_prev_time", 0) < 1.0):
            return False, "⏳ Please wait a moment before going back again."
        player._prev_lock = True
        player._last_prev_time = now

        try:
            # 1. Smart Replay: If currently playing song has been running for > 7 seconds,
            # restart it from 0:00 (industry standard music player behavior).
            pos_ms = player.position or 0
            if player.current and pos_ms > 7000:
                await player.seek(0)
                await self._update_panel(player, track=player.current)
                return True, "⏮️ Replaying current song from start."

            # 2. Check History
            history = getattr(player.queue, "history", None)
            if not history or history.is_empty:
                return False, "⏮️ No previous songs in history."

            history_list = list(history)
            current = getattr(player, "current", None)

            # Find the true previous track from history
            prev_track = None
            for t in reversed(history_list):
                if not current or t.identifier != current.identifier or t.uri != current.uri:
                    prev_track = t
                    break

            if not prev_track:
                # If only identical track is in history, use the last one
                prev_track = history_list[-1]

            # Re-queue the current track at index 0 so pressing Skip goes forward back to it!
            if current:
                try:
                    player.queue.put_at(0, current)
                except Exception:
                    pass

            # Update track position counter for the playlist display
            if player.guild:
                guild_id = player.guild.id
                current_pos = self._track_position.get(guild_id, 1)
                self._track_position[guild_id] = max(1, current_pos - 1)
                self._suppress_next_increment.add(guild_id)

            display_name = getattr(prev_track, "_title_override", prev_track.title)
            await player.play(prev_track)
            await self._update_panel(player, track=prev_track)
            return True, f"⏮️ Playing previous track: **{display_name}**"
        except Exception as e:
            logger.error(f"Error executing previous: {e}", exc_info=True)
            return False, f"❌ Failed to go back: `{e}`"
        finally:
            player._prev_lock = False

    @commands.command(name="skip", aliases=["s", "next"])
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def skip(self, ctx: commands.Context):
        """Skip to the next song in queue."""
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            return await ctx.send("I am not in a voice channel.", delete_after=5)
        success, msg = await self._execute_skip(vc)
        await ctx.send(msg, delete_after=4 if success else 6)
        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass

    @commands.command(name="previous", aliases=["prev", "back"])
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def previous(self, ctx: commands.Context):
        """Go back to the previous song or replay current song."""
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            return await ctx.send("I am not in a voice channel.", delete_after=5)
        success, msg = await self._execute_previous(vc)
        await ctx.send(msg, delete_after=4 if success else 6)
        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass


    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context):
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("Nothing is playing.")
        q = list(vc.queue)
        lines = [f"**Now Playing:** {vc.current.title}\n"]
        if not q:
            lines.append("*Queue is empty.*")
        else:
            for i, t in enumerate(q[:20], 1):
                lines.append(f"`{i}.` {t.title}")
            if len(q) > 20:
                lines.append(f"*...and {len(q) - 20} more tracks.*")
        embed = discord.Embed(title="Queue", description="\n".join(lines), color=0x0284c7)
        await ctx.send(embed=embed)

    @commands.command(name="seek", aliases=["ff", "jump"])
    async def seek(self, ctx: commands.Context, position: str):
        """
        Seek to a specific timestamp in the song.
        Usage: !seek 1:30 or !seek 90
        """
        vc: wavelink.Player = ctx.voice_client
        if not _player_valid(vc) or not vc.current:
            return await ctx.send("Nothing is currently playing.", delete_after=5)

        # Parse position (mm:ss, hh:mm:ss, or seconds)
        ms = None
        try:
            if ":" in position:
                parts = position.split(":")
                if len(parts) == 2:
                    ms = (int(parts[0]) * 60 + int(parts[1])) * 1000
                elif len(parts) == 3:
                    ms = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
            else:
                ms = int(position) * 1000
        except ValueError:
            ms = None

        if ms is None or ms < 0:
            return await ctx.send("Invalid format! Use `!seek 1:30` or `!seek 90`.", delete_after=5)

        if not vc.current.is_seekable or vc.current.length == 0:
            return await ctx.send("❌ Cannot seek on a live stream.", delete_after=5)

        if ms > vc.current.length:
            return await ctx.send("Timestamp exceeds song length!", delete_after=5)

        await vc.seek(ms)
        await ctx.send(f"⏩ Seeked to **{position}**", delete_after=5)
        try:
            await ctx.message.delete(delay=3)
        except Exception:
            pass



    # -----------------------------------------------------
    # Custom Playlists
    # -----------------------------------------------------
    @commands.group(name="playlist", aliases=["pl"], invoke_without_command=True)
    async def playlist(self, ctx: commands.Context):
        """Manage your custom playlists."""
        await ctx.send("Usage: `!playlist save <name>`, `!playlist load <name>`, `!playlist list`, `!playlist delete <name>`", delete_after=10)
        
    @playlist.command(name="save")
    async def playlist_save(self, ctx: commands.Context, *, name: str):
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("Nothing is playing right now.", delete_after=5)
            
        uris = [vc.current.uri]
        for track in vc.queue:
            uris.append(track.uri)
            
        if len(uris) > 100: # Limit playlist size
            uris = uris[:100]
            
        success = await db._run(db.save_playlist, ctx.guild.id, ctx.author.id, name, uris)
        if success:
            await ctx.send(f"✅ Saved **{len(uris)}** tracks to playlist `{name}`!", delete_after=10)
        else:
            await ctx.send(f"❌ You already have a playlist named `{name}`. Delete it first.", delete_after=10)
            
    @playlist.command(name="list")
    async def playlist_list(self, ctx: commands.Context):
        names = await db._run(db.list_playlists, ctx.guild.id, ctx.author.id)
        if not names:
            return await ctx.send("You don't have any saved playlists.", delete_after=10)
            
        embed = discord.Embed(title=f"📜 {ctx.author.display_name}'s Playlists", description="\n".join(f"• `{n}`" for n in names), color=0x2b2d31)
        await ctx.send(embed=embed, delete_after=30)
        
    @playlist.command(name="load")
    async def playlist_load(self, ctx: commands.Context, *, name: str):
        uris = await db._run(db.load_playlist, ctx.guild.id, ctx.author.id, name)
        if not uris:
            return await ctx.send(f"❌ Playlist `{name}` not found.", delete_after=5)
            
        channel = await _ensure_author_in_voice(ctx, "❌ You need to be in a voice channel first.")
        if not channel:
            return
            
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            try:
                vc = await _connect_voice_with_retry(ctx.author.voice.channel, bot=self.bot)
            except wavelink.exceptions.InvalidNodeException:
                return await ctx.send("❌ **The Lavalink Music Server is currently offline or restarting. We will fix this soon!**", delete_after=60.0)
            except VoiceChannelPermissionError as e:
                return await ctx.send(f"❌ **Missing voice channel permissions:** {e}", delete_after=60)
            except Exception as e:
                return await ctx.send(f"❌ **Voice connection timeout or error:** {e}", delete_after=60)
            vc.home = ctx.channel
            vc.autoplay = wavelink.AutoPlayMode.disabled
            vc.inactive_timeout = None  # Prevent auto-disconnect
            
        msg = await ctx.send(f"🔍 Loading {len(uris)} tracks from `{name}`...")
        
        # ── Concurrent Fetching ──────────────────────────────────────────────
        sem = asyncio.Semaphore(5)
        
        async def fetch_track(idx: int, uri: str):
            async with sem:
                try:
                    tracks = await wavelink.Playable.search(uri)
                    if tracks:
                        t = tracks[0] if isinstance(tracks, list) else (tracks.tracks[0] if hasattr(tracks, 'tracks') else tracks)
                        return (idx, t)
                except Exception:
                    pass
                return (idx, None)
                
        tasks = [fetch_track(i, uri) for i, uri in enumerate(uris)]
        results = await asyncio.gather(*tasks)
        
        # Add to queue in original playlist order
        count = 0
        for _, track in sorted(results, key=lambda x: x[0]):
            if track:
                await vc.queue.put_wait(track)
                count += 1
                
        if not (vc.playing or vc.paused) and not vc.queue.is_empty:
            next_track = vc.queue.get()
            try:
                vc = await self._safe_play(vc, next_track)
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                await ctx.send(f"❌ **Playback couldn't recover:** {e}")
            
        await msg.edit(content=f"✅ Loaded **{count}** tracks from playlist `{sanitize_md(name)}`!")
        
    @playlist.command(name="delete")
    async def playlist_delete(self, ctx: commands.Context, *, name: str):
        deleted = await db._run(db.delete_playlist, ctx.guild.id, ctx.author.id, name)
        if deleted:
            await ctx.send(f"✅ Deleted playlist `{name}`.", delete_after=10)
        else:
            await ctx.send(f"❌ Playlist `{name}` not found.", delete_after=5)


    @commands.command(name="dashboardbobo", aliases=["dashboard", "web", "bobodashboard"])
    async def dashboardbobo(self, ctx: commands.Context):
        """Generate a link to the Interactive Glassmorphism Web Dashboard."""
        if not ctx.guild:
            return await ctx.send("❌ This command must be used in a server.")
            
        import os
        port = os.environ.get("SERVER_PORT", os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "7927")))
        base_url = os.environ.get("DASHBOARD_URL") or f"http://160.191.77.60:{port}"
        dashboard_link = f"{base_url}/?guild={ctx.guild.id}"
        
        embed = discord.Embed(
            title="🌐 BOBO 2026 Web Player",
            description=(
                f"Control music playback, view the live queue, search tracks, and adjust volume/filters in real time from your browser!\n\n"
                f"👉 **[Click Here to Open Web Player]({dashboard_link})**"
            ),
            color=0x0284c7
        )
        embed.set_footer(text="Nexus Music Engine • High-Performance Audio")
        
        view = discord.ui.View()
        button = discord.ui.Button(label="Open Web Player", style=discord.ButtonStyle.link, url=dashboard_link, emoji="🌐")
        view.add_item(button)
        
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(MusicCog(bot))









