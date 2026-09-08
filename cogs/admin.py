"""
cogs/admin.py
=============
Admin & moderation cog for Nexus bot.

Commands:
    prefix      — Change the bot command prefix for this server
    setdj       — Set a DJ role (only that role can use music commands)
    removedj    — Remove DJ role restriction
    blacklist   — Blacklist a user from using the bot
    unblacklist — Remove a user from the blacklist
    botinfo     — Show bot statistics and info
    ping        — Check bot latency
    reload      — Reload a cog (owner only)
    shutdown    — Safely shut down the bot (owner only)
    purge       — Delete messages in bulk
    forceleave  — Force bot to leave voice (admin only)
    announce    — Send an embed announcement to a channel
"""

import logging
import os
import platform
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

logger = logging.getLogger("nexus.admin")

# In-memory blacklist (guild_id → set of user_ids)
# For persistence, swap this with a JSON/database store
_blacklists: dict[int, set[int]] = {}

from utils.checks import set_dj_role, remove_dj_role
from utils.mod_config import set_log_channel, log_action

# Bot start time for uptime calculation
import time
_start_time = time.time()

# Embed colors
EMBED_COLOR   = discord.Color.from_rgb(114, 137, 218)
ERROR_COLOR   = discord.Color.from_rgb(237, 66, 69)
SUCCESS_COLOR = discord.Color.from_rgb(87, 242, 135)
WARNING_COLOR = discord.Color.from_rgb(254, 231, 92)

def is_blacklisted(user_id: int, guild_id: int) -> bool:
    """Check if a user is blacklisted in a given guild."""
    return user_id in _blacklists.get(guild_id, set())


class AdminCog(commands.Cog, name="Admin"):
    """
    Server administration commands for Nexus bot.
    Most commands here require Manage Guild or Administrator permissions.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Check: User not blacklisted ───────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Global check for all commands in this cog — block blacklisted users."""
        if ctx.guild and is_blacklisted(ctx.author.id, ctx.guild.id):
            await ctx.send(
                embed=discord.Embed(
                    description="🚫 You are blacklisted from using Nexus in this server.",
                    color=ERROR_COLOR,
                )
            )
            return False
        return True

    # ── Bot Info ──────────────────────────────────────────────────────────────────

    @commands.command(name="ping", aliases=["latency", "status"])
    async def ping_cmd(self, ctx: commands.Context) -> None:
        """
        Show the bot's latency to various global servers.
        Usage: !ping
        """
        # Initial message
        embed = discord.Embed(
            title="⏳ Measuring Global Latency...",
            description="Pinging voice regions and search APIs. Please wait...",
            color=0x2b2d31
        )
        msg = await ctx.send(embed=embed)

        discord_ping = round(self.bot.latency * 1000)

        import aiohttp
        import time

        async def measure_ping(url: str) -> str:
            try:
                start_time = time.perf_counter()
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=3):
                        pass
                end_time = time.perf_counter()
                latency = round((end_time - start_time) * 1000)
                return f"{latency}ms"
            except Exception:
                return "Timeout"

        def format_status(ping_str):
            if ping_str == "Timeout":
                return "🔴 Offline"
            try:
                ping_val = int(ping_str.replace("ms", ""))
                if ping_val < 150: return "🟢 Operational"
                elif ping_val < 300: return "🟡 Degraded"
                else: return "🟠 High Latency"
            except Exception:
                return "🔴 Error"

        try:
            # Pinging endpoints representing different regions
            sl_ping = await measure_ping("https://www.dialog.lk")
            sg_ping = await measure_ping("https://www.amazon.sg")
            us_ping = await measure_ping("https://www.nytimes.com")
            eu_ping = await measure_ping("https://www.bbc.co.uk")
            yt_ping = await measure_ping("https://www.youtube.com")

            # Build final embed
            final_embed = discord.Embed(
                title="🌐 Global System Status",
                color=0x2b2d31
            )
            
            final_embed.add_field(
                name="🇱🇰 Voice Servers - Sri Lanka",
                value=f"`{sl_ping}` latency • {format_status(sl_ping)}",
                inline=False
            )
            final_embed.add_field(
                name="🇸🇬 Voice Servers - Asia",
                value=f"`{sg_ping}` latency • {format_status(sg_ping)}",
                inline=False
            )
            final_embed.add_field(
                name="🇺🇸 Voice Servers - US East",
                value=f"`{us_ping}` latency • {format_status(us_ping)}",
                inline=False
            )
            final_embed.add_field(
                name="🇪🇺 Voice Servers - Europe",
                value=f"`{eu_ping}` latency • {format_status(eu_ping)}",
                inline=False
            )
            final_embed.add_field(
                name="🔍 Search API (YouTube/Spotify)",
                value=f"`{yt_ping}` latency • {format_status(yt_ping)}",
                inline=False
            )
            
            # Add Lavalink Node stats
            import wavelink
            if hasattr(wavelink, 'Pool') and hasattr(wavelink.Pool, 'nodes') and wavelink.Pool.nodes:
                node_text = ""
                for node in wavelink.Pool.nodes.values():
                    status = "🟢 Connected" if getattr(node, 'status', None) == wavelink.NodeStatus.CONNECTED else "🔴 Disconnected"
                    players = getattr(node.stats, 'playing_players', 0) if getattr(node, 'stats', None) else 0
                    node_text += f"**{node.identifier}**: {status} ({players} active streams)\n"
                
                final_embed.add_field(
                    name="🎵 Lavalink Music Nodes",
                    value=node_text,
                    inline=False
                )
            else:
                final_embed.add_field(
                    name="🎵 Lavalink Music Nodes",
                    value="🔴 No nodes connected.",
                    inline=False
                )

            final_embed.set_footer(text=f"Bot Gateway Ping: {discord_ping}ms")
            await msg.edit(content=None, embed=final_embed)
        except Exception as e:
            await msg.edit(content=f"An error occurred while pinging:\n```py\n{e}\n```", embed=None)

    @commands.command(name="botinfo", aliases=["info", "about"])
    async def botinfo_cmd(self, ctx: commands.Context) -> None:
        """
        Display bot statistics and system info.
        Usage: !botinfo
        """
        # Calculate uptime
        uptime_sec = int(time.time() - _start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        total_members = sum(g.member_count or 0 for g in self.bot.guilds)

        embed = discord.Embed(
            title="🤖 Nexus Music Bot — Info",
            description="A powerful Discord music bot built with discord.py and yt-dlp.",
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Owner", value=f"<@{self.bot.owner_id}>" if self.bot.owner_id else "Unknown", inline=True)
        embed.add_field(name="🏓 Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="⏱ Uptime", value=uptime_str, inline=True)
        embed.add_field(name="🌐 Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="👥 Members", value=str(total_members), inline=True)
        embed.add_field(name="🐍 Python", value=platform.python_version(), inline=True)
        embed.add_field(name="📦 discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="💻 OS", value=f"{platform.system()} {platform.release()}", inline=True)
        embed.add_field(name="🔧 Prefix", value=f"`{self.bot.command_prefix}`", inline=True)

        if self.bot.user and self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)

        embed.set_footer(text="Nexus Music Bot • Made with ❤️")
        await ctx.send(embed=embed)

    # ── Server Configuration ──────────────────────────────────────────────────────

    @commands.command(name="setprefix", aliases=["prefix"])
    @commands.has_permissions(manage_guild=True)
    async def setprefix_cmd(self, ctx: commands.Context, new_prefix: str) -> None:
        """
        Change the bot's command prefix for this server.
        (Note: This implementation changes it globally. For per-server prefixes,
         integrate a database.)

        Usage: !setprefix ?
        """
        if len(new_prefix) > 5:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Prefix must be 5 characters or fewer.",
                    color=ERROR_COLOR,
                )
            )
            return

        self.bot.command_prefix = new_prefix
        await ctx.send(
            embed=discord.Embed(
                description=f"✅ Prefix changed to `{new_prefix}`",
                color=SUCCESS_COLOR,
            )
        )
        await log_action(self.bot, ctx.guild, discord.Embed(title="⚙️ Prefix Changed", description=f"New prefix: `{new_prefix}`\nBy: {ctx.author.mention}", color=WARNING_COLOR))

    @commands.command(name="setlogchannel")
    @commands.has_permissions(manage_guild=True)
    async def setlogchannel_cmd(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel for Action Logging (Audit Trail)."""
        set_log_channel(ctx.guild.id, channel.id)
        await ctx.send(embed=discord.Embed(description=f"📝 Action Logging channel set to {channel.mention}", color=SUCCESS_COLOR))

    @commands.command(name="setdj")
    @commands.has_permissions(manage_guild=True)
    async def setdj_cmd(self, ctx: commands.Context, role: discord.Role) -> None:
        """
        Set a DJ role. Only members with this role can use music commands.
        Admins are always exempt from this restriction.

        Usage: !setdj @DJ
        """
        set_dj_role(ctx.guild.id, role.id)
        await ctx.send(
            embed=discord.Embed(
                description=f"🎧 DJ role set to **{role.name}**. Only this role can use music commands.",
                color=SUCCESS_COLOR,
            )
        )

    @commands.command(name="removedj", aliases=["cleardj"])
    @commands.has_permissions(manage_guild=True)
    async def removedj_cmd(self, ctx: commands.Context) -> None:
        """
        Remove the DJ role restriction. All members can use music commands.
        Usage: !removedj
        """
        remove_dj_role(ctx.guild.id)
        await ctx.send(
            embed=discord.Embed(
                description="✅ DJ role removed. All members can now use music commands.",
                color=SUCCESS_COLOR,
            )
        )

    # ── Blacklist ──────────────────────────────────────────────────────────────────

    @commands.command(name="blacklist", aliases=["block"])
    @commands.has_permissions(manage_guild=True)
    async def blacklist_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        """
        Blacklist a member from using Nexus in this server.
        Usage: !blacklist @user
        """
        if member.guild_permissions.administrator:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Cannot blacklist an administrator.",
                    color=ERROR_COLOR,
                )
            )
            return

        if ctx.guild.id not in _blacklists:
            _blacklists[ctx.guild.id] = set()

        _blacklists[ctx.guild.id].add(member.id)
        await ctx.send(
            embed=discord.Embed(
                description=f"🚫 **{member.display_name}** has been blacklisted from using Nexus.",
                color=SUCCESS_COLOR,
            )
        )
        await log_action(self.bot, ctx.guild, discord.Embed(title="🚫 User Blacklisted", description=f"User: {member.mention}\nBy: {ctx.author.mention}", color=ERROR_COLOR))

    @commands.command(name="unblacklist", aliases=["unblock"])
    @commands.has_permissions(manage_guild=True)
    async def unblacklist_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        """
        Remove a member from the blacklist.
        Usage: !unblacklist @user
        """
        guild_bl = _blacklists.get(ctx.guild.id, set())
        if member.id not in guild_bl:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ **{member.display_name}** is not blacklisted.",
                    color=ERROR_COLOR,
                )
            )
            return

        guild_bl.discard(member.id)
        await ctx.send(
            embed=discord.Embed(
                description=f"✅ **{member.display_name}** has been removed from the blacklist.",
                color=SUCCESS_COLOR,
            )
        )
        await log_action(self.bot, ctx.guild, discord.Embed(title="✅ User Unblacklisted", description=f"User: {member.mention}\nBy: {ctx.author.mention}", color=SUCCESS_COLOR))

    @commands.command(name="blacklistview", aliases=["blocklist"])
    @commands.has_permissions(manage_guild=True)
    async def blacklistview_cmd(self, ctx: commands.Context) -> None:
        """
        View all blacklisted users in this server.
        Usage: !blacklistview
        """
        guild_bl = _blacklists.get(ctx.guild.id, set())
        if not guild_bl:
            await ctx.send(
                embed=discord.Embed(
                    description="✅ No users are blacklisted in this server.",
                    color=SUCCESS_COLOR,
                )
            )
            return

        lines = []
        for uid in guild_bl:
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"Unknown ({uid})"
            lines.append(f"• {name} (`{uid}`)")

        embed = discord.Embed(
            title=f"🚫 Blacklisted Users — {ctx.guild.name}",
            description="\n".join(lines),
            color=WARNING_COLOR,
        )
        await ctx.send(embed=embed)

    # ── Message Moderation ────────────────────────────────────────────────────────

    @commands.command(name="purge", aliases=["prune", "deletemsg"])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge_cmd(self, ctx: commands.Context, amount: int) -> None:
        """
        Delete a number of messages from this channel (1–100).
        Usage: !purge 10
        """
        if not 1 <= amount <= 100:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Amount must be between 1 and 100.",
                    color=ERROR_COLOR,
                )
            )
            return

        # Delete the command message itself too
        deleted = await ctx.channel.purge(limit=amount + 1)
        confirm = await ctx.send(
            embed=discord.Embed(
                description=f"🗑️ Deleted **{len(deleted) - 1}** message(s).",
                color=SUCCESS_COLOR,
            )
        )
        await log_action(self.bot, ctx.guild, discord.Embed(title="🗑️ Messages Purged", description=f"Channel: {ctx.channel.mention}\nAmount: {len(deleted) - 1}\nBy: {ctx.author.mention}", color=WARNING_COLOR))
        # Auto-delete confirmation after 4 seconds
        await confirm.delete(delay=4)

    # ── Voice Admin ────────────────────────────────────────────────────────────────

    @commands.command(name="forceleave", aliases=["fdc", "fdisconnect"])
    @commands.has_permissions(administrator=True)
    async def forceleave_cmd(self, ctx: commands.Context) -> None:
        """
        Force the bot to leave the voice channel and clear queue.
        Admin only. Use when !leave is not responding.

        Usage: !forceleave
        """
        voice_client = ctx.guild.voice_client
        if not voice_client:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ I'm not in a voice channel!",
                    color=ERROR_COLOR,
                )
            )
            return

        channel_name = voice_client.channel.name

        # BUG FIX #9: MusicCog doesn't have _players/_destroy_player.
        # wavelink players are accessed via the Pool; clear the queue and
        # disconnect via the wavelink Player if available, then force-disconnect.
        import wavelink
        music_cog = self.bot.get_cog("music")
        if music_cog:
            # Remove guild from 24/7 set if present
            if hasattr(music_cog, "_247_guilds"):
                music_cog._247_guilds.discard(ctx.guild.id)

        # Disconnect through wavelink player if one is active (clears queue etc.)
        if isinstance(voice_client, wavelink.Player):
            try:
                voice_client.queue.clear()
                await voice_client.disconnect()
            except Exception:
                await voice_client.disconnect(force=True)
        else:
            await voice_client.disconnect(force=True)

        await ctx.send(
            embed=discord.Embed(
                description=f"💪 Force disconnected from **{channel_name}**.",
                color=SUCCESS_COLOR,
            )
        )

    # ── Announcements ──────────────────────────────────────────────────────────────

    @commands.command(name="announce", aliases=["say"])
    @commands.has_permissions(manage_guild=True)
    async def announce_cmd(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        *,
        message: str,
    ) -> None:
        """
        Send an embed announcement to a channel.
        Usage: !announce #channel Your announcement text here
        """
        embed = discord.Embed(
            description=message,
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=ctx.guild.name,
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None,
        )
        embed.set_footer(text=f"Announced by {ctx.author.display_name}")

        try:
            await channel.send(embed=embed)
            await ctx.send(
                embed=discord.Embed(
                    description=f"✅ Announcement sent to {channel.mention}",
                    color=SUCCESS_COLOR,
                )
            )
        except discord.Forbidden:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ I don't have permission to send messages in {channel.mention}",
                    color=ERROR_COLOR,
                )
            )

    # ── Owner-Only Commands ────────────────────────────────────────────────────────

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload_cmd(self, ctx: commands.Context, cog: str) -> None:
        """
        Reload a cog by name (owner only). Useful for hot-reloading without restart.
        Usage: !reload music
        """
        extension = f"cogs.{cog.lower()}"
        try:
            await self.bot.reload_extension(extension)
            await ctx.send(
                embed=discord.Embed(
                    description=f"♻️ Reloaded `{extension}` successfully.",
                    color=SUCCESS_COLOR,
                )
            )
            logger.info(f"Reloaded extension: {extension}")
        except commands.ExtensionNotLoaded:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Extension `{extension}` is not loaded.",
                    color=ERROR_COLOR,
                )
            )
        except Exception as e:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Failed to reload `{extension}`:\n```{e}```",
                    color=ERROR_COLOR,
                )
            )
            logger.error(f"Reload failed for {extension}: {e}", exc_info=True)

    @commands.command(name="reloadall")
    @commands.is_owner()
    async def reloadall_cmd(self, ctx: commands.Context) -> None:
        """
        Reload all cogs at once (owner only).
        Usage: !reloadall
        """
        results = []
        for ext in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ `{ext}`")
            except Exception as e:
                results.append(f"❌ `{ext}`: {e}")

        embed = discord.Embed(
            title="♻️ Reload All Cogs",
            description="\n".join(results),
            color=EMBED_COLOR,
        )
        await ctx.send(embed=embed)

    @commands.command(name="shutdown", aliases=["poweroff", "die"])
    @commands.is_owner()
    async def shutdown_cmd(self, ctx: commands.Context) -> None:
        """
        Safely shut down the bot (owner only).
        Usage: !shutdown
        """
        await ctx.send(
            embed=discord.Embed(
                description="👋 Shutting down Nexus... Goodbye!",
                color=WARNING_COLOR,
            )
        )
        logger.info(f"Shutdown requested by owner {ctx.author}.")
        await self.bot.close()

    @commands.command(name="servers")
    @commands.is_owner()
    async def servers_cmd(self, ctx: commands.Context) -> None:
        """
        List all servers the bot is in (owner only).
        Usage: !servers
        """
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        lines = [
            f"`{i+1}.` **{g.name}** — {g.member_count} members (ID: `{g.id}`)"
            for i, g in enumerate(guilds[:20])
        ]
        embed = discord.Embed(
            title=f"🌐 Connected Servers ({len(guilds)})",
            description="\n".join(lines) if lines else "No servers.",
            color=EMBED_COLOR,
        )
        await ctx.send(embed=embed)

    # ── Error Handlers ─────────────────────────────────────────────────────────────

    @setprefix_cmd.error
    @setdj_cmd.error
    @removedj_cmd.error
    @blacklist_cmd.error
    @unblacklist_cmd.error
    @purge_cmd.error
    @forceleave_cmd.error
    @announce_cmd.error
    async def admin_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Unified error handler for admin commands."""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    description="🚫 You don't have permission to use this command.",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    description=f"🚫 I'm missing permissions: `{error.missing_permissions}`",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Member not found. Mention them or use their ID.",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.RoleNotFound):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Role not found. Mention the role or check its name.",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Channel not found. Mention the channel.",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.NotOwner):
            await ctx.send(
                embed=discord.Embed(
                    description="🚫 Only the bot owner can use this command.",
                    color=ERROR_COLOR,
                )
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Missing argument: `{error.param.name}`\nUse `!help {ctx.command.name}` for usage.",
                    color=ERROR_COLOR,
                )
            )
        else:
            logger.error(f"Admin cog error: {error}", exc_info=True)
            raise error


# ─── Cog Setup ────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    """Entry point called by bot.load_extension()."""
    await bot.add_cog(AdminCog(bot))
    logger.info("AdminCog loaded successfully.")
