# utils/mod_config.py
import discord

# In-memory storage for guild settings.
# guild_id -> channel_id
_log_channels: dict[int, int] = {}

def set_log_channel(guild_id: int, channel_id: int) -> None:
    _log_channels[guild_id] = channel_id

def get_log_channel(guild_id: int) -> int | None:
    return _log_channels.get(guild_id)

async def log_action(bot: discord.Client, guild: discord.Guild, embed: discord.Embed) -> None:
    """Send an embed to the configured log channel, if any."""
    channel_id = get_log_channel(guild.id)
    if not channel_id:
        return
        
    channel = guild.get_channel(channel_id)
    if not channel:
        return
        
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass
