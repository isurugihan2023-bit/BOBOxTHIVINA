"""
Nexus Discord Music Bot
=======================
Main entry point for the Nexus bot. Handles bot initialization,
cog loading, event listeners, and graceful shutdown.

Author: Nexus Bot
Version: 1.0.0
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ─── Logging Setup ──────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    """Configure structured logging with color support and file output."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Root logger
    logger = logging.getLogger("nexus")
    logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Console handler with color formatting
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)

    # File handler for persistent logs
    file_handler = logging.FileHandler(
        log_dir / "nexus.log", encoding="utf-8", mode="a"
    )
    file_handler.setLevel(logging.INFO)

    # Formatter
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(fmt)
    file_handler.setFormatter(fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # Suppress noisy discord.py debug logs unless explicitly set
    if log_level != "DEBUG":
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.ERROR)

    return logger


logger = setup_logging()

# ─── Bot Configuration ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
BOT_PREFIX = os.getenv("PREFIX") or os.getenv("BOT_PREFIX") or "!"
OWNER_ID = os.getenv("OWNER_ID")

if not DISCORD_TOKEN:
    logger.critical(
        "DISCORD_TOKEN is not set in .env! Please configure your token and retry."
    )
    sys.exit(1)


# ─── Intents ────────────────────────────────────────────────────────────────────
# Setup bot intents
intents = discord.Intents.default()
intents.message_content = True       # Read message content for prefix commands
intents.voice_states = True          # Track who is in voice channels
intents.guilds = True                # Access guild data
intents.members = True               # Access member data for permission checks


# ─── Bot Subclass ────────────────────────────────────────────────────────────────
class NexusBot(commands.Bot):
    """
    Custom Bot subclass for Nexus.

    Extends commands.Bot to add:
    - Automatic cog loading from the /cogs directory
    - Startup banner logging
    - Graceful shutdown handling
    - Global error handling
    """

    def __init__(self) -> None:
        super().__init__(
            command_prefix=BOT_PREFIX,
            intents=intents,
            help_command=None,
            owner_id=int(OWNER_ID) if OWNER_ID else None,
            case_insensitive=True,
            strip_after_prefix=True,
            chunk_guilds_at_startup=False, # Optimizes memory and startup speed significantly
        )
        self.logger = logging.getLogger("nexus.bot")

    async def setup_hook(self) -> None:
        """Called automatically before the bot starts. Load all cogs here."""
        self.session = aiohttp.ClientSession()
        self.logger.info("Global aiohttp ClientSession initialised.")

        # BUG FIX #13: Initialise the database asynchronously before loading cogs.
        from utils.db import init_db
        await init_db()
        self.logger.info("Database initialised.")

        self.logger.info("Loading cogs...")
        cog_dir = Path("cogs")

        if not cog_dir.exists():
            self.logger.error(f"Cogs directory '{cog_dir}' not found!")
            return

        # Dynamically load all .py files in the cogs/ directory
        loaded, failed = 0, 0
        for cog_file in sorted(cog_dir.glob("*.py")):
            # Skip __init__.py and any private files
            if cog_file.name.startswith("_"):
                continue

            extension = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(extension)
                self.logger.info(f"  ✓ Loaded: {extension}")
                loaded += 1
            except Exception as e:
                self.logger.error(f"  ✗ Failed to load {extension}: {e}", exc_info=True)
                failed += 1

        self.logger.info(f"Cog loading complete — {loaded} loaded, {failed} failed.")
        
        # Sync the command tree to Discord to register all slash commands globally
        try:
            # synced = await self.tree.sync()
            self.logger.info("Command tree sync skipped/commented out.")
        except Exception as e:
            self.logger.error(f"Failed to sync commands: {e}")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        content = message.content
        prefix = self.command_prefix

        if isinstance(prefix, str) and content.startswith(prefix):
            body = content[len(prefix):]
            if body.lower().startswith("play") and len(body) > 4 and body[4] != " ":
                first_word = body.split()[0].lower()
                cmd = self.get_command(first_word)
                if not cmd:
                    remainder = body[4:]
                    message.content = f"{prefix}play {remainder}"

        await self.process_commands(message)


    async def on_ready(self) -> None:
        """Called when the bot has connected and is ready to operate."""
        self.logger.info("=" * 60)
        self.logger.info(f"  Nexus Bot is online!")
        self.logger.info(f"  Logged in as: {self.user} (ID: {self.user.id})")
        self.logger.info(f"  Connected to : {len(self.guilds)} guild(s)")
        self.logger.info(f"  Prefix       : '{BOT_PREFIX}'")
        self.logger.info(f"  discord.py   : {discord.__version__}")
        self.logger.info("=" * 60)

        # Set a rich presence status for the bot
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"music | {BOT_PREFIX}help",
            )
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """
        Global error handler for all command errors.
        Specific handlers in cogs take precedence; this handles anything that falls through.
        """
        # Ignore errors that have already been handled in a local handler
        if hasattr(ctx.command, "on_error") or hasattr(ctx.cog, "cog_command_error"):
            return

        # Unwrap CommandInvokeError to get the original exception
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            return  # Silently ignore unknown commands

        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"❌ Missing argument: `{error.param.name}`\n"
                f"Use `{BOT_PREFIX}help {ctx.command.name}` for usage info."
            )

        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {error}")

        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(f"🚫 You don't have permission to use this command.")

        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(f"🚫 I don't have permission to do that: `{error.missing_permissions}`")

        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ This command can only be used in a server.")

        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏳ Slow down! Try again in **{error.retry_after:.1f}s**."
            )

        else:
            # Log unexpected errors for debugging
            self.logger.error(
                f"Unhandled command error in '{ctx.command}': {error}",
                exc_info=error,
            )
            await ctx.send(f"⚠️ An unexpected error occurred. Please try again later.")

    async def close(self) -> None:
        """Gracefully shut down the bot, cleaning up voice connections."""
        self.logger.info("Shutting down Nexus Bot...")
        # Disconnect from all voice channels before closing
        for vc in self.voice_clients:
            await vc.disconnect(force=True)
        if hasattr(self, "session") and self.session and not self.session.closed:
            await self.session.close()
        await super().close()
        self.logger.info("Nexus Bot has shut down cleanly.")


# ─── Entry Point ─────────────────────────────────────────────────────────────────
async def main() -> None:
    """Initialize and run the Nexus bot."""
    bot = NexusBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt. Goodbye!")
    except discord.LoginFailure:
        logger.critical("Invalid Discord token! Check your .env file.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Fatal error during startup: {e}", exc_info=True)
        sys.exit(1)
