import os
import glob
import asyncio
import ctypes.util

import discord
from discord.ext import commands
import yt_dlp

TOKEN = os.environ["DISCORD_TOKEN"]


def load_opus():
    if discord.opus.is_loaded():
        return
    candidates = [ctypes.util.find_library("opus")]
    candidates += glob.glob("/nix/store/*opus*/lib/libopus.so*")
    candidates += ["libopus.so.0", "libopus.so"]
    for path in candidates:
        if not path:
            continue
        try:
            discord.opus.load_opus(path)
            if discord.opus.is_loaded():
                print("Opus loaded:", path)
                return
        except Exception:
            continue
    print("WARNING: Opus not loaded, voice may fail")


YDL_OPTS = {"format": "bestaudio/best", "noplaylist": True, "quiet": True}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
queues = {}  # guild_id -> list of queries


def resolve(query):
    q = query.strip()
    if q.startswith("http"):
        if "soundcloud.com" not in q:
            raise ValueError("ساوند كلاود فقط")
        return q
    return f"scsearch1:{q}"


def fetch_stream(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(resolve(query), download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info["url"], info["title"]


async def play_next(ctx):
    q = queues.get(ctx.guild.id, [])
    vc = ctx.voice_client
    if not q or not vc:
        return
    query = q.pop(0)
    try:
        url, title = await bot.loop.run_in_executor(None, fetch_stream, query)
    except Exception as e:
        await ctx.send(f"❌ فشل: {e}")
        return await play_next(ctx)

    def after(err):
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    vc.play(discord.FFmpegPCMAudio(url, **FFMPEG_OPTS), after=after)
    await ctx.send(f"🎶 الآن: **{title}**")


@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    if not ctx.author.voice:
        return await ctx.send("ادخل روم صوتي أول.")
    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()
    queues.setdefault(ctx.guild.id, []).append(query)
    vc = ctx.voice_client
    if vc.is_playing() or vc.is_paused():
        await ctx.send("➕ انضافت للطابور")
    else:
        await play_next(ctx)


@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ تم التخطي")


@bot.command()
async def pause(ctx):
    if ctx.voice_client:
        ctx.voice_client.pause()


@bot.command()
async def resume(ctx):
    if ctx.voice_client:
        ctx.voice_client.resume()


@bot.command()
async def stop(ctx):
    queues[ctx.guild.id] = []
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("⏹️ تم الإيقاف")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


load_opus()
bot.run(TOKEN)
