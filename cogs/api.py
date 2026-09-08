import base64
import hashlib
import hmac
import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
import time
import asyncio
import os
import json
import logging
import wavelink
import utils.db as db

logger = logging.getLogger("nexus.api")

BOBO_FAVICON_BYTES = base64.b64decode("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")


class APICog(commands.Cog):
    """
    High-Performance Interactive Web Player Dashboard & REST API
    Features:
    - Glassmorphism Single-Page Web Application
    - Real-time Bidirectional WebSocket Sync
    - Remote Play/Pause, Seek, Volume, Skip, Filter Controls
    - In-Browser Search & Queue Management
    """

    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.runner = None
        self.start_time = time.time()
        self.websockets: dict[int, set[web.WebSocketResponse]] = {}
        self.global_latencies = {
            "sl": "Measuring...",
            "sg": "Measuring...",
            "us": "Measuring...",
            "eu": "Measuring...",
            "yt": "Measuring...",
            "lavalink": "Measuring..."
        }
        self._admin_user: str | None = None
        self._admin_pass: str | None = None
        self._auth_secret: str | None = None

        # Setup Routes
        self.app.router.add_get('/', self.handle_dashboard)
        self.app.router.add_get('/dashboard', self.handle_dashboard)
        self.app.router.add_get('/favicon.ico', self.handle_favicon)
        self.app.router.add_get('/favicon.png', self.handle_favicon)
        self.app.router.add_post('/api/login', self.handle_login)
        self.app.router.add_post('/api/verify', self.handle_verify)
        self.app.router.add_get('/stats', self.handle_stats)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/guilds', self.handle_get_guilds)
        self.app.router.add_get('/api/player/{guild_id}', self.handle_get_player)
        self.app.router.add_post('/api/player/{guild_id}/control', self.handle_player_control)
        self.app.router.add_post('/api/player/{guild_id}/filter', self.handle_player_filter)
        self.app.router.add_post('/api/player/{guild_id}/queue/add', self.handle_queue_add)
        self.app.router.add_post('/api/player/{guild_id}/queue/remove', self.handle_queue_remove)
        self.app.router.add_post('/api/player/{guild_id}/queue/play_index', self.handle_queue_play_index)
        self.app.router.add_get('/api/player/{guild_id}/queue/search', self.handle_queue_search)
        self.app.router.add_post('/api/player/{guild_id}/radio', self.handle_radio_play)
        self.app.router.add_post('/api/player/{guild_id}/resume', self.handle_resume_session)
        self.app.router.add_get('/ws/{guild_id}', self.handle_websocket)

        self.latency_updater.start()
        self.state_sync_loop.start()

    async def measure_ping(self, url: str) -> str:
        try:
            start_time = time.perf_counter()
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=3):
                    pass
            end_time = time.perf_counter()
            return str(round((end_time - start_time) * 1000))
        except Exception:
            return "Offline"

    @tasks.loop(seconds=1.5)
    async def state_sync_loop(self):
        """Continuously broadcast real-time song, position and queue to all connected web dashboards."""
        for guild_id in list(self.websockets.keys()):
            if self.websockets.get(guild_id):
                try:
                    await self.broadcast_state(guild_id)
                except Exception:
                    pass

    @state_sync_loop.before_loop
    async def before_state_sync_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2)
    async def latency_updater(self):
        try:
            self.global_latencies["sl"] = await self.measure_ping("https://www.dialog.lk")
            self.global_latencies["sg"] = await self.measure_ping("https://www.amazon.sg")
            self.global_latencies["us"] = await self.measure_ping("https://www.nytimes.com")
            self.global_latencies["eu"] = await self.measure_ping("https://www.bbc.co.uk")
            self.global_latencies["yt"] = await self.measure_ping("https://www.youtube.com")
            
            # Measure active Lavalink node ping
            try:
                active_node = wavelink.Pool.nodes.get("Primary-Node") or next(
                    (n for n in wavelink.Pool.nodes.values() if n.status == wavelink.NodeStatus.CONNECTED),
                    None
                )
                if active_node and active_node.uri:
                    self.global_latencies["lavalink"] = await self.measure_ping(active_node.uri)
                else:
                    self.global_latencies["lavalink"] = "Offline"
            except Exception:
                self.global_latencies["lavalink"] = "Offline"
        except Exception as e:
            logger.debug(f"Latency updater error: {e}")

    def _get_system_status(self) -> dict:
        """Returns live status and latency metrics for Discord Bot and Lavalink Server."""
        active_node = wavelink.Pool.nodes.get("Primary-Node") or next(
            (n for n in wavelink.Pool.nodes.values() if n.status == wavelink.NodeStatus.CONNECTED),
            None
        )
        lavalink_online = bool(active_node and active_node.status == wavelink.NodeStatus.CONNECTED)
        node_id = active_node.identifier if active_node else "Offline"
        lavalink_ping = self.global_latencies.get("lavalink", "N/A")
        
        bot_ping = round(self.bot.latency * 1000) if self.bot.latency else 0

        return {
            "bot": {
                "online": True,
                "ping": bot_ping
            },
            "lavalink": {
                "online": lavalink_online,
                "identifier": node_id,
                "ping": lavalink_ping
            }
        }

    # ── State Serializer ────────────────────────────────────────────────────────
    def get_player_state(self, guild_id: int) -> dict:
        guild = self.bot.get_guild(guild_id)
        system_status = self._get_system_status()
        if not guild:
            return {"error": "Guild not found", "is_connected": False, "system_status": system_status}

        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return {
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "guild_icon": str(guild.icon.url) if guild.icon else None,
                "is_connected": False,
                "is_playing": False,
                "is_paused": False,
                "volume": 100,
                "position": 0,
                "duration": 0,
                "loop_mode": "off",
                "active_filter": None,
                "current_track": None,
                "queue": [],
                "queue_count": 0,
                "system_status": system_status,
                "voice_members": []
            }

        music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
        active_filter = music_cog._active_filter_names.get(guild.id) if music_cog else None

        loop_mode = "off"
        if hasattr(player, "queue"):
            if player.queue.mode == wavelink.QueueMode.loop:
                loop_mode = "track"
            elif player.queue.mode == wavelink.QueueMode.loop_all:
                loop_mode = "queue"

        current_dict = None
        if player.current:
            t = player.current
            current_dict = {
                "title": getattr(t, '_title_override', t.title),
                "author": t.author,
                "uri": t.uri,
                "artwork": t.artwork or "",
                "length": t.length or 0,
                "is_stream": bool(getattr(t, "is_stream", False))
            }

        queue_list = []
        if hasattr(player, "queue"):
            for i, track in enumerate(list(player.queue)[:250]):
                queue_list.append({
                    "index": i,
                    "title": getattr(track, '_title_override', track.title),
                    "author": track.author,
                    "uri": track.uri,
                    "artwork": track.artwork or "",
                    "length": track.length or 0
                })

        is_radio = bool(getattr(player.current, "_is_radio", False)) if player.current else False
        has_saved_playlist = bool(getattr(player, "_saved_playlist_session", None))

        # Extract human voice channel members
        voice_members = []
        vc = getattr(player, "channel", None)
        if vc and hasattr(vc, "members"):
            for m in vc.members:
                if not m.bot:
                    avatar_url = str(m.display_avatar.url) if hasattr(m, "display_avatar") and m.display_avatar else (str(m.avatar.url) if m.avatar else "")
                    voice_members.append({
                        "id": str(m.id),
                        "name": m.display_name,
                        "avatar": avatar_url
                    })

        return {
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "guild_icon": str(guild.icon.url) if guild.icon else None,
            "is_connected": True,
            "is_playing": bool(player.playing),
            "is_paused": bool(player.paused),
            "is_radio": is_radio,
            "has_saved_playlist": has_saved_playlist,
            "volume": getattr(player, "volume", 100),
            "position": getattr(player, "position", 0),
            "duration": (player.current.length or 0) if player.current else 0,
            "loop_mode": loop_mode,
            "active_filter": active_filter,
            "current_track": current_dict,
            "queue": queue_list,
            "queue_count": len(player.queue) if hasattr(player, "queue") else 0,
            "system_status": system_status,
            "voice_members": voice_members
        }

    # ── Live WebSocket Broadcaster ──────────────────────────────────────────────
    async def broadcast_state(self, guild_id: int):
        """Broadcast live player state to all connected web clients."""
        if guild_id not in self.websockets or not self.websockets[guild_id]:
            return
        state = self.get_player_state(guild_id)
        payload = json.dumps({"type": "STATE_UPDATE", "data": state})
        dead_ws = set()
        for ws in list(self.websockets.get(guild_id, [])):
            try:
                await ws.send_str(payload)
            except Exception:
                dead_ws.add(ws)
        if guild_id in self.websockets:
            self.websockets[guild_id].difference_update(dead_ws)

    # ── Handlers ────────────────────────────────────────────────────────────────
    async def handle_stats(self, request):
        servers = len(self.bot.guilds)
        members = sum(guild.member_count for guild in self.bot.guilds if guild.member_count)
        uptime_seconds = int(time.time() - self.start_time)
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        uptime_str = f"{days}d {hours}h" if days > 0 else f"{hours}h {minutes}m"
        if uptime_seconds < 3600:
            uptime_str = f"{minutes}m {uptime_seconds % 60}s"

        ping = f"{round(self.bot.latency * 1000)}ms" if self.bot.latency else "N/A"

        return web.json_response({
            "servers": servers,
            "members": members,
            "uptime": uptime_str,
            "ping": ping,
            "latencies": self.global_latencies
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_get_guilds(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guilds_data = []
        for guild in self.bot.guilds:
            player = getattr(guild, "voice_client", None)
            is_active = bool(player and isinstance(player, wavelink.Player) and player.current)
            guilds_data.append({
                "id": str(guild.id),
                "name": guild.name,
                "icon": str(guild.icon.url) if guild.icon else None,
                "members": guild.member_count,
                "is_active": is_active,
                "now_playing": getattr(player.current, "title", None) if is_active else None
            })
        return web.json_response(guilds_data, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_get_player(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        state = self.get_player_state(guild_id)
        return web.json_response(state, headers={"Access-Control-Allow-Origin": "*"})

    async def _safe_update_discord_panel(self, player):
        try:
            if not player or not hasattr(player, "panel_message") or not player.panel_message:
                return
            music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
            if not music_cog:
                return
            if hasattr(music_cog, "_update_panel"):
                await music_cog._update_panel(player)
            elif player.current:
                from .music import build_now_playing_embed, MusicController
                embed = build_now_playing_embed(player, player.current)
                view = MusicController(player)
                await player.panel_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.debug(f"Discord panel silent update: {e}")

    async def handle_player_control(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)

        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        action = data.get("action")

        try:
            if action == "play_pause":
                if player.paused:
                    await player.pause(False)
                elif player.playing:
                    await player.pause(True)
                elif not player.queue.is_empty:
                    next_track = player.queue.get()
                    await player.play(next_track)
                else:
                    return web.json_response({
                        "success": False,
                        "error": "Nothing is playing and queue is empty. Search a song to start!"
                    }, headers={"Access-Control-Allow-Origin": "*"})
            elif action == "skip":
                music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
                if music_cog and hasattr(music_cog, "_execute_skip"):
                    await music_cog._execute_skip(player)
                elif not player.queue.is_empty:
                    next_t = player.queue.get()
                    await player.play(next_t)
                else:
                    await player.skip(force=True)
            elif action in ("radio_off", "restore_playlist"):
                await self.restore_saved_playlist(player)
            elif action in ("back", "previous"):
                music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
                if music_cog and hasattr(music_cog, "_execute_previous"):
                    await music_cog._execute_previous(player)
            elif action == "stop":
                music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
                if music_cog and hasattr(music_cog, "save_session_for_guild"):
                    try:
                        await music_cog.save_session_for_guild(guild_id, player)
                    except Exception as e:
                        logger.debug(f"Failed to save session on api stop: {e}")
                await player.disconnect()
            elif action == "seek":
                pos_ms = int(data.get("position", 0))
                await player.seek(pos_ms)
                return web.json_response({"success": True, "position": pos_ms}, headers={"Access-Control-Allow-Origin": "*"})
            elif action == "volume":
                vol = max(0, min(100, int(data.get("volume", 100))))
                await player.set_volume(vol)
                return web.json_response({"success": True, "volume": vol}, headers={"Access-Control-Allow-Origin": "*"})
            elif action == "shuffle":
                if hasattr(player, "queue"):
                    player.queue.shuffle()
            elif action == "loop":
                mode = data.get("mode", "toggle")
                if mode == "toggle":
                    if player.queue.mode == wavelink.QueueMode.normal:
                        player.queue.mode = wavelink.QueueMode.loop
                    elif player.queue.mode == wavelink.QueueMode.loop:
                        player.queue.mode = wavelink.QueueMode.loop_all
                    else:
                        player.queue.mode = wavelink.QueueMode.normal
                elif mode == "off":
                    player.queue.mode = wavelink.QueueMode.normal
                elif mode == "track":
                    player.queue.mode = wavelink.QueueMode.loop
                elif mode == "queue":
                    player.queue.mode = wavelink.QueueMode.loop_all

            # Update panel embed in Discord safely if present
            if hasattr(player, "panel_message") and player.panel_message:
                asyncio.create_task(self._safe_update_discord_panel(player))

            await self.broadcast_state(guild_id)
            return web.json_response({"success": True, "state": self.get_player_state(guild_id)}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            logger.error(f"Error executing control action {action}: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_player_filter(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        filter_name = data.get("filter", "clear")
        filters = wavelink.Filters()
        music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")

        if filter_name == "clear":
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "clear"
        elif filter_name == "bassboost":
            filters.equalizer.set(band=0, gain=0.20)
            filters.equalizer.set(band=1, gain=0.15)
            filters.equalizer.set(band=2, gain=0.10)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "bassboost"
        elif filter_name == "lofi":
            filters.timescale.set(speed=0.92, pitch=0.92, rate=1.0)
            filters.low_pass.set(smoothing=20.0)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "lofi"
        elif filter_name == "nightcore":
            filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "nightcore"
        elif filter_name == "vaporwave":
            filters.timescale.set(speed=0.80, pitch=0.80, rate=1.0)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "vaporwave"
        elif filter_name == "8d":
            filters.rotation.set(rotation_hz=0.2)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "8d"
        elif filter_name == "karaoke":
            filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
            await player.set_filters(filters)
            if music_cog:
                music_cog._active_filters[guild_id] = filters
                music_cog._active_filter_names[guild_id] = "karaoke"

        await self.broadcast_state(guild_id)
        return web.json_response({"success": True, "filter": filter_name}, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_queue_add(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        query = data.get("query", "").strip()
        play_next = bool(data.get("play_next", False))
        play_now = bool(data.get("play_now", False))

        if not query:
            return web.json_response({"success": False, "error": "Empty query"}, status=400)

        search_query = query if (query.startswith("http://") or query.startswith("https://") or ":" in query) else f"ytsearch:{query}"
        try:
            tracks = await wavelink.Playable.search(search_query)
            if not tracks:
                tracks = await wavelink.Playable.search(f"ytsearch:{query}")
            if not tracks:
                return web.json_response({"success": False, "error": "No tracks found"}, status=404)

            if isinstance(tracks, list):
                track = tracks[0]
            elif hasattr(tracks, "tracks") and tracks.tracks:
                track = tracks.tracks[0]
            else:
                track = tracks

            if play_now:
                await player.play(track)
            elif not player.playing and player.queue.is_empty:
                await player.play(track)
            else:
                if play_next:
                    player.queue.put_at(0, track)
                else:
                    await player.queue.put_wait(track)

            await self.broadcast_state(guild_id)
            return web.json_response({"success": True, "track": {"title": track.title, "author": track.author, "uri": track.uri}}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_queue_remove(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        index = int(data.get("index", -1))
        if 0 <= index < len(player.queue):
            try:
                player.queue.delete(index)
                await self.broadcast_state(guild_id)
                return web.json_response({"success": True}, headers={"Access-Control-Allow-Origin": "*"})
            except Exception as e:
                return web.json_response({"success": False, "error": str(e)}, status=500)
        return web.json_response({"success": False, "error": "Invalid index"}, status=400)

    async def handle_queue_play_index(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        index = int(data.get("index", -1))
        if 0 <= index < len(player.queue):
            track = player.queue[index]
            remaining = list(player.queue)[index + 1:]
            player.queue.clear()
            if remaining:
                player.queue.put(remaining)
            music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
            if music_cog:
                if guild_id in music_cog._track_position:
                    music_cog._track_position[guild_id] += (index + 1)
                music_cog._suppress_next_increment.add(guild_id)
            await player.play(track)
            await self.broadcast_state(guild_id)
            return web.json_response({"success": True}, headers={"Access-Control-Allow-Origin": "*"})
        return web.json_response({"success": False, "error": "Invalid index"}, status=400)

    async def handle_queue_search(self, request):
        """Ultra-fast search across ALL tracks in the queue regardless of queue size."""
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        q = request.query.get("q", "").strip().lower()
        if not q:
            return web.json_response({"success": True, "results": [], "total_queue": len(player.queue) if hasattr(player, 'queue') else 0})

        results = []
        if hasattr(player, "queue"):
            for i, track in enumerate(list(player.queue)):
                title = getattr(track, '_title_override', track.title) or ""
                author = track.author or ""
                if q in title.lower() or q in author.lower():
                    results.append({
                        "index": i,
                        "title": title,
                        "author": author,
                        "uri": track.uri,
                        "artwork": getattr(track, 'artwork', ""),
                        "length": track.length or 0
                    })
                    if len(results) >= 100:
                        break

        return web.json_response({
            "success": True, 
            "results": results, 
            "total_matches": len(results),
            "total_queue": len(player.queue) if hasattr(player, 'queue') else 0
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def restore_saved_playlist(self, player: wavelink.Player) -> bool:
        session = getattr(player, "_saved_playlist_session", None)
        if not session:
            return False

        setattr(player, "_saved_playlist_session", None)
        track = session.get("track")
        pos = session.get("position", 0)
        remaining_queue = session.get("queue", [])

        player.queue.clear()
        if remaining_queue:
            player.queue.put(remaining_queue)

        if track:
            await player.play(track)
            if pos > 1000 and getattr(track, "length", 0) and pos < track.length:
                async def _seek_resume():
                    await asyncio.sleep(0.8)
                    try:
                        if player.playing and player.current == track:
                            await player.seek(pos)
                    except Exception as e:
                        logger.debug(f"Resume seek error: {e}")
                asyncio.create_task(_seek_resume())

        if player.guild:
            await self.broadcast_state(player.guild.id)
        return True

    async def handle_radio_play(self, request):
        if not self._validate_token(request):
            return self._unauthorized()
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Guild not found"}, status=404)
        player: wavelink.Player | None = getattr(guild, "voice_client", None)
        if not player or not isinstance(player, wavelink.Player):
            return web.json_response({"success": False, "error": "Player not connected"}, status=400)

        data = await request.json()
        station_url = data.get("url", "").strip()
        station_name = data.get("name", "Live Radio").strip()

        # Seamlessly turn off radio & restore playlist from stop location
        if station_url in ("off", "stop", "restore") or data.get("action") == "off":
            restored = await self.restore_saved_playlist(player)
            if restored:
                return web.json_response({"success": True, "restored": True, "message": "Resumed playlist from stop location!"}, headers={"Access-Control-Allow-Origin": "*"})
            else:
                await player.stop()
                await self.broadcast_state(guild_id)
                return web.json_response({"success": True, "restored": False, "message": "Radio turned off"}, headers={"Access-Control-Allow-Origin": "*"})

        if not station_url:
            return web.json_response({"success": False, "error": "No station URL"}, status=400)

        try:
            tracks = await wavelink.Playable.search(station_url)
            if not tracks:
                return web.json_response({"success": False, "error": "Could not connect to this station"}, status=404)

            track = tracks[0] if isinstance(tracks, list) else (tracks.tracks[0] if hasattr(tracks, 'tracks') else tracks)
            setattr(track, "_title_override", station_name)
            setattr(track, "_is_radio", True)

            # SAVE PLAYLIST SESSION BEFORE SWITCHING TO RADIO
            current_track = player.current
            is_current_radio = getattr(current_track, "_is_radio", False)
            if not is_current_radio and (current_track or not player.queue.is_empty):
                saved_session = {
                    "track": current_track,
                    "position": getattr(player, "position", 0),
                    "queue": list(player.queue)
                }
                setattr(player, "_saved_playlist_session", saved_session)
                logger.info(f"Saved playlist session for guild {guild_id}: track={getattr(current_track, 'title', None)}, queue_len={len(saved_session['queue'])}, pos={saved_session['position']}")

            player.queue.clear()
            await player.play(track)
            await self.broadcast_state(guild_id)
            return web.json_response({"success": True, "station": station_name}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            logger.error(f"Radio API error: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_resume_session(self, request):
        if not self._validate_token(request):
            return self._unauthorized()

        try:
            guild_id = int(request.match_info["guild_id"])
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return web.json_response({"success": False, "error": "Guild not found"}, status=404, headers={"Access-Control-Allow-Origin": "*"})

            session = await db._run(db.get_last_session, guild_id)
            if not session or (not session.get("current_track_uri") and not session.get("queue_uris")):
                return web.json_response(
                    {"success": False, "error": "No previous session found to resume for this server.", "reason": "no_session"},
                    status=200,
                    headers={"Access-Control-Allow-Origin": "*"}
                )

            target_channel_id = session.get("voice_channel_id")
            target_channel = guild.get_channel(target_channel_id) if target_channel_id else None

            # Verify channel still exists and is a voice channel
            if not target_channel or not isinstance(target_channel, (discord.VoiceChannel, discord.StageChannel)):
                return web.json_response({
                    "success": False,
                    "error": "The last voice channel no longer exists.",
                    "reason": "channel_empty_or_missing"
                }, status=200, headers={"Access-Control-Allow-Origin": "*"})

            # Verify channel has at least one human member
            human_members = [m for m in target_channel.members if not m.bot]
            if len(human_members) == 0:
                return web.json_response({
                    "success": False,
                    "error": f"Cannot resume: Voice channel '{target_channel.name}' is currently empty.",
                    "reason": "channel_empty_or_missing"
                }, status=200, headers={"Access-Control-Allow-Origin": "*"})

            music_cog = self.bot.get_cog("Music") or self.bot.get_cog("music")
            if not music_cog or not hasattr(music_cog, "restore_session_for_guild"):
                return web.json_response(
                    {"success": False, "error": "Music cog restore not available"},
                    status=500,
                    headers={"Access-Control-Allow-Origin": "*"}
                )

            text_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
            success, msg, player = await music_cog.restore_session_for_guild(guild, target_channel, text_channel)
            if not success:
                return web.json_response(
                    {"success": False, "error": msg},
                    status=200,
                    headers={"Access-Control-Allow-Origin": "*"}
                )

            await self.broadcast_state(guild_id)
            return web.json_response(
                {"success": True, "message": msg},
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error(f"Error handling resume session: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    async def handle_websocket(self, request):
        guild_id = int(request.match_info["guild_id"])
        ws = web.WebSocketResponse()
        try:
            await ws.prepare(request)
        except Exception:
            return ws

        token = request.query.get("token", "")
        if not self._token_matches(token):
            await ws.close(code=aiohttp.WSCloseCode.POLICY_VIOLATION, message=b"Unauthorized")
            return ws

        if guild_id not in self.websockets:
            self.websockets[guild_id] = set()
        self.websockets[guild_id].add(ws)

        try:
            # Send initial state
            state = self.get_player_state(guild_id)
            await ws.send_str(json.dumps({"type": "STATE_UPDATE", "data": state}))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data == "ping":
                        await ws.send_str("pong")
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
        except (ConnectionResetError, aiohttp.ClientConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug(f"WebSocket client disconnected: {e}")
        finally:
            if guild_id in self.websockets:
                self.websockets[guild_id].discard(ws)
        return ws

    # ── Auth helpers ─────────────────────────────────────────────────────────────
    # Credentials are read from environment at startup in cog_load().
    # _admin_user / _admin_pass / _auth_secret are set there; if any are missing
    # the bot refuses to start (loud failure, no silent defaults).

    def _get_auth_token(self) -> str:
        if not (self._admin_user and self._admin_pass and self._auth_secret):
            return ""
        return hashlib.sha256(
            f"{self._admin_user}:{self._admin_pass}:{self._auth_secret}".encode()
        ).hexdigest()

    def _token_matches(self, token: str) -> bool:
        """Constant-time token validation against current server-side token."""
        if not token:
            return False
        expected = self._get_auth_token()
        return bool(expected and hmac.compare_digest(token, expected))

    def _validate_token(self, request) -> bool:
        """Return True only if the request carries the correct server-side token.

        Accepts two locations (in priority order):
          1. Authorization: Bearer <token>  header  (used by all fetch() calls)
          2. bobo_auth cookie               (legacy browser cookie path)
        """
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return self._token_matches(auth_header[7:].strip())
        cookie_token = request.cookies.get("bobo_auth", "")
        if cookie_token:
            return self._token_matches(cookie_token.strip())
        return False

    def _unauthorized(self):
        return web.json_response(
            {"success": False, "error": "Unauthorized"},
            status=401,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def handle_login(self, request):
        try:
            data = await request.json()
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", "")).strip()
            if username == self._admin_user and password == self._admin_pass:
                token = self._get_auth_token()
                resp = web.json_response({"success": True, "token": token}, headers={"Access-Control-Allow-Origin": "*"})
                resp.set_cookie("bobo_auth", token, max_age=30*86400, samesite="Lax")
                return resp
            return web.json_response({"success": False, "error": "Invalid username or password"}, status=401, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_verify(self, request):
        try:
            data = await request.json()
            token = data.get("token", "")
            if token == self._get_auth_token():
                return web.json_response({"success": True, "valid": True}, headers={"Access-Control-Allow-Origin": "*"})
            return web.json_response({"success": False, "valid": False}, status=401, headers={"Access-Control-Allow-Origin": "*"})
        except Exception:
            return web.json_response({"success": False, "valid": False}, status=401, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_dashboard(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

    async def handle_favicon(self, request):
        if BOBO_FAVICON_BYTES:
            return web.Response(body=BOBO_FAVICON_BYTES, content_type='image/png')
        return web.Response(status=404)

    async def cog_load(self):
        # ── Priority 1 / S3: Hard failure if credentials missing from .env ─────────
        self._admin_user = os.getenv("DASHBOARD_ADMIN_USER")
        self._admin_pass = os.getenv("DASHBOARD_ADMIN_PASS")
        self._auth_secret = os.getenv("DASHBOARD_AUTH_SECRET")
        missing = [v for v, val in [
            ("DASHBOARD_ADMIN_USER", self._admin_user),
            ("DASHBOARD_ADMIN_PASS", self._admin_pass),
            ("DASHBOARD_AUTH_SECRET", self._auth_secret),
        ] if not val]

        if missing:
            logger.critical(
                f"❌ CRITICAL: Dashboard credentials missing from .env: {', '.join(missing)}. "
                "Refusing to start web dashboard server without valid credentials."
            )
            return

        await self.start_web_server()

    async def start_web_server(self):
        if not (self._admin_user and self._admin_pass and self._auth_secret):
            logger.critical("❌ Web dashboard server startup aborted: credentials not configured in .env.")
            return

        if not self.runner:
            try:
                self.runner = web.AppRunner(self.app)
                await self.runner.setup()
                port = int(os.environ.get('PORT', os.environ.get('DASHBOARD_PORT', os.environ.get('SERVER_PORT', 7927))))
                site = web.TCPSite(self.runner, '0.0.0.0', port)
                await site.start()
                logger.info(f"🌐 Glassmorphism Web Player Dashboard live on port {port}")
            except Exception as e:
                logger.error(f"Error starting dashboard web server: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.start_web_server()

    async def cog_unload(self):
        self.latency_updater.cancel()
        if self.runner:
            await self.runner.cleanup()


from .dashboard_template import DASHBOARD_HTML

async def setup(bot):
    await bot.add_cog(APICog(bot))
