import discord
from discord.ext import commands

# Per-guild DJ role IDs (guild_id → role_id or None)
# In production, move this to a database (e.g., SQLite via utils/db.py)
_dj_roles: dict[int, int] = {}


def set_dj_role(guild_id: int, role_id: int) -> None:
    _dj_roles[guild_id] = role_id


def remove_dj_role(guild_id: int) -> None:
    _dj_roles.pop(guild_id, None)


def get_dj_role(guild_id: int) -> int | None:
    return _dj_roles.get(guild_id)


def has_dj_role():
    """
    BUG FIX #12: Actually enforce the DJ role restriction.

    Rules:
    - If no DJ role is set for the guild → everyone can use music commands.
    - If a DJ role IS set → the user must have that role OR have Administrator.
    - In DMs (no guild) → always allow.
    """
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True  # Allow in DMs

        # Admins are always exempt
        if ctx.author.guild_permissions.administrator:
            return True

        dj_role_id = get_dj_role(ctx.guild.id)
        if dj_role_id is None:
            return True  # No restriction set — everyone allowed

        # Check whether the member has the DJ role
        has_role = any(role.id == dj_role_id for role in ctx.author.roles)
        if not has_role:
            await ctx.send(
                embed=discord.Embed(
                    description="🎧 You need the DJ role to use music commands.",
                    color=discord.Color.from_rgb(237, 66, 69),
                )
            )
        return has_role

    return commands.check(predicate)
