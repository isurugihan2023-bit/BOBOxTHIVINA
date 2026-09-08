import sqlite3
from typing import Optional
import os
import json
import asyncio
import concurrent.futures

DB_PATH = "data/nexus.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Thread-pool executor for running blocking SQLite calls off the event loop
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")

# Persistent global connection for performance optimization
# check_same_thread=False is safe because we use max_workers=1
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL;")
_conn.execute("PRAGMA synchronous=NORMAL;")

def get_connection():
    return _conn

def _init_db_sync():
    """Blocking DB initialisation — always run via asyncio.run_in_executor."""
    conn = get_connection()
    c = conn.cursor()
    
    # Playlists table
    c.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(guild_id, owner_id, name)
        )
    ''')
    
    # Playlist tracks table
    c.execute('''
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            uri TEXT NOT NULL,
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        )
    ''')

    # Guild settings table
    c.execute('''
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            dj_role_id INTEGER,
            mode_247 INTEGER DEFAULT 0
        )
    ''')

    # User Theme Songs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_themes (
            guild_id INTEGER,
            user_id INTEGER,
            url TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    
    # Aliases table
    c.execute('''
        CREATE TABLE IF NOT EXISTS aliases (
            query TEXT PRIMARY KEY,
            correct_query TEXT NOT NULL
        )
    ''')

    # Autoplay Modes table
    c.execute('''
        CREATE TABLE IF NOT EXISTS autoplay_modes (
            guild_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'strict'
        )
    ''')

    # Vibe Cache table
    c.execute('''
        CREATE TABLE IF NOT EXISTS vibe_cache (
            guild_id TEXT,
            vibe_query TEXT,
            resolved_track_uri TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, vibe_query)
        )
    ''')

    # Skip Feedback table (with 14-day decay logic applied on read)
    c.execute('''
        CREATE TABLE IF NOT EXISTS skip_feedback (
            guild_id TEXT,
            artist TEXT,
            genre TEXT,
            skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Last Session persistence table for explicit resume
    c.execute('''
        CREATE TABLE IF NOT EXISTS last_sessions (
            guild_id INTEGER PRIMARY KEY,
            voice_channel_id INTEGER,
            current_track_uri TEXT,
            position_ms INTEGER DEFAULT 0,
            volume INTEGER DEFAULT 100,
            filter_name TEXT,
            track_position INTEGER DEFAULT 0,
            track_total INTEGER DEFAULT 0,
            queue_uris TEXT,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    # DO NOT CLOSE


async def init_db():
    """
    BUG FIX #13: init_db is now async and runs the blocking SQLite work on a
    thread-pool executor so it never blocks the event loop.
    Call this once from NexusBot.setup_hook() instead of at import time.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _init_db_sync)


# ---------------------------------------------------------------------------
# Async-safe helper – runs any blocking DB function on the thread-pool
# ---------------------------------------------------------------------------
async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)

def set_247(guild_id: int, enabled: bool):
    """Set 24/7 mode for a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO guild_settings (guild_id, mode_247) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET mode_247 = ?",
        (guild_id, int(enabled), int(enabled))
    )
    conn.commit()

def get_247(guild_id: int) -> bool:
    """Get 24/7 mode status for a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT mode_247 FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    if row and row[0]:
        return True
    return False

def set_dj_role(guild_id: int, role_id: int):
    """Set the DJ role ID for a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO guild_settings (guild_id, dj_role_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET dj_role_id = ?",
        (guild_id, role_id, role_id)
    )
    conn.commit()

def get_dj_role(guild_id: int) -> Optional[int]:
    """Get the DJ role ID for a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT dj_role_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    if row:
        return row[0]
    return None

def remove_dj_role(guild_id: int):
    """Remove the DJ role for a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO guild_settings (guild_id, dj_role_id) VALUES (?, NULL) ON CONFLICT(guild_id) DO UPDATE SET dj_role_id = NULL",
        (guild_id,)
    )
    conn.commit()

def set_theme(guild_id: int, user_id: int, url: str):
    """Set a theme song for a user in a guild."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_themes (guild_id, user_id, url) VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET url = ?",
        (guild_id, user_id, url, url)
    )
    conn.commit()

def get_theme(guild_id: int, user_id: int) -> Optional[str]:
    """Get the theme song for a user."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT url FROM user_themes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = c.fetchone()
    if row:
        return row[0]
    return None

def remove_theme(guild_id: int, user_id: int):
    """Remove a theme song for a user."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM user_themes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    conn.commit()

def save_playlist(guild_id: int, owner_id: int, name: str, uris: list[str]) -> bool:
    """Save a playlist. Returns True if successful, False if it already exists."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO playlists (guild_id, owner_id, name) VALUES (?, ?, ?)', 
                  (guild_id, owner_id, name.lower()))
        playlist_id = c.lastrowid
        
        # Insert all tracks
        for pos, uri in enumerate(uris):
            c.execute('INSERT INTO playlist_tracks (playlist_id, position, uri) VALUES (?, ?, ?)',
                      (playlist_id, pos, uri))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def load_playlist(guild_id: int, owner_id: int, name: str) -> list[str]:
    """Returns a list of track URIs."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT pt.uri FROM playlist_tracks pt
        JOIN playlists p ON pt.playlist_id = p.id
        WHERE p.guild_id = ? AND p.owner_id = ? AND p.name = ?
        ORDER BY pt.position ASC
    ''', (guild_id, owner_id, name.lower()))
    rows = c.fetchall()
    return [row[0] for row in rows]

def list_playlists(guild_id: int, owner_id: int) -> list[str]:
    """Returns a list of playlist names for a user."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT name FROM playlists WHERE guild_id = ? AND owner_id = ?', (guild_id, owner_id))
    rows = c.fetchall()
    return [row[0] for row in rows]

def delete_playlist(guild_id: int, owner_id: int, name: str) -> bool:
    """Delete a playlist. Returns True if deleted, False if not found."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM playlists WHERE guild_id = ? AND owner_id = ? AND name = ?', (guild_id, owner_id, name.lower()))
    deleted = c.rowcount > 0
    conn.commit()
    return deleted

def get_alias(query: str) -> Optional[str]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT correct_query FROM aliases WHERE query = ?", (query.lower(),))
    row = c.fetchone()
    return row[0] if row else None

def set_alias(query: str, correct_query: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO aliases (query, correct_query) VALUES (?, ?) ON CONFLICT(query) DO UPDATE SET correct_query = ?",
        (query.lower(), correct_query, correct_query)
    )
    conn.commit()

def get_autoplay_mode(guild_id: int) -> str:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT mode FROM autoplay_modes WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    return row[0] if row else "strict"

def set_autoplay_mode(guild_id: int, mode: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO autoplay_modes (guild_id, mode) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET mode = ?",
        (guild_id, mode, mode)
    )
    conn.commit()

def add_skip_feedback(guild_id: str, artist: str, genre: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO skip_feedback (guild_id, artist, genre, skipped_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (str(guild_id), artist, genre)
    )
    conn.commit()

def get_skip_feedback(guild_id: str, limit: int = 10) -> list[dict]:
    conn = get_connection()
    c = conn.cursor()
    # Only get skips from the last 14 days
    c.execute("SELECT artist, genre FROM skip_feedback WHERE guild_id = ? AND skipped_at >= datetime('now', '-14 days') ORDER BY skipped_at DESC LIMIT ?", (str(guild_id), limit))
    rows = c.fetchall()
    return [{"artist": row[0], "genre": row[1]} for row in rows]

def get_vibe_cache(guild_id: str, vibe_query: str, max_age_hours: int = 24) -> Optional[str]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT resolved_track_uri FROM vibe_cache WHERE guild_id = ? AND vibe_query = ? AND last_updated >= datetime('now', ?)", 
        (str(guild_id), vibe_query.lower(), f"-{max_age_hours} hours")
    )
    row = c.fetchone()
    return row[0] if row else None

def set_vibe_cache(guild_id: str, vibe_query: str, resolved_track_uri: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO vibe_cache (guild_id, vibe_query, resolved_track_uri, last_updated) VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(guild_id, vibe_query) DO UPDATE SET resolved_track_uri = ?, last_updated = CURRENT_TIMESTAMP",
        (str(guild_id), vibe_query.lower(), resolved_track_uri, resolved_track_uri)
    )
    conn.commit()


def save_last_session(
    guild_id: int,
    voice_channel_id: int,
    current_track_uri: str,
    position_ms: int,
    volume: int,
    filter_name: str,
    track_position: int,
    track_total: int,
    queue_uris: list[str]
):
    """Save or overwrite the last session for a guild upon intentional stop/disconnect."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO last_sessions (
            guild_id, voice_channel_id, current_track_uri, position_ms, volume,
            filter_name, track_position, track_total, queue_uris, saved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(guild_id) DO UPDATE SET
            voice_channel_id=excluded.voice_channel_id,
            current_track_uri=excluded.current_track_uri,
            position_ms=excluded.position_ms,
            volume=excluded.volume,
            filter_name=excluded.filter_name,
            track_position=excluded.track_position,
            track_total=excluded.track_total,
            queue_uris=excluded.queue_uris,
            saved_at=CURRENT_TIMESTAMP
    ''', (
        guild_id, voice_channel_id, current_track_uri, position_ms, volume,
        filter_name, track_position, track_total, json.dumps(queue_uris)
    ))
    conn.commit()


def get_last_session(guild_id: int) -> Optional[dict]:
    """Retrieve the last saved session for a guild, if any."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT guild_id, voice_channel_id, current_track_uri, position_ms, volume,
               filter_name, track_position, track_total, queue_uris, saved_at
        FROM last_sessions WHERE guild_id = ?
    ''', (guild_id,))
    row = c.fetchone()
    if not row:
        return None
    try:
        q_uris = json.loads(row[8]) if row[8] else []
    except Exception:
        q_uris = []
    return {
        "guild_id": row[0],
        "voice_channel_id": row[1],
        "current_track_uri": row[2],
        "position_ms": row[3],
        "volume": row[4],
        "filter_name": row[5],
        "track_position": row[6],
        "track_total": row[7],
        "queue_uris": q_uris,
        "saved_at": row[9]
    }


