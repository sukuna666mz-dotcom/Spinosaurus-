import os
import time
import glob
import random
import asyncio
import ctypes.util
from collections import Counter

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

TOKEN = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")  # optional, enables /play autocomplete


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

queues = {}          # guild_id -> list of (query, source) tuples
volumes = {}         # guild_id -> float (0.1 - 1.5)
now_playing = {}     # guild_id -> title
now_source = {}      # guild_id -> "YouTube" or "SoundCloud" label for the current track
now_duration = {}    # guild_id -> total duration in seconds (int) or None
panels = {}          # guild_id -> panel message
current_query = {}   # guild_id -> (query, source) tuple of the track currently/last playing
loop_mode = {}       # guild_id -> bool, True = repeat current track forever
play_counts = {}     # guild_id -> Counter({title: times_played})
start_time = {}      # guild_id -> time.monotonic() when the current track started
paused_at = {}       # guild_id -> time.monotonic() when it was paused (absent if not paused)
paused_total = {}    # guild_id -> accumulated paused seconds for the current track
now_url = {}         # guild_id -> cached stream URL of the current track (used for seeking)
now_thumbnail = {}   # guild_id -> thumbnail URL of the current track
now_meta = {}        # guild_id -> {"guild":, "channel":, "query":, "src":} for the current track
seeking_flags = set()  # guild_ids currently mid-seek (so the `after` callback doesn't advance the queue)


BLOCKED_DOMAINS = [
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
    "redtube.com", "youporn.com", "spankbang.com", "tnaflix.com",
    "motherless.com", "chaturbate.com", "onlyfans.com", "stripchat.com",
    "brazzers.com", "txxx.com", "beeg.com", "tube8.com", "4tube.com",
    "porntube.com", "eporner.com",
]


def is_blocked(url: str) -> bool:
    u = url.strip().lower()
    return any(domain in u for domain in BLOCKED_DOMAINS)


def resolve(query, source="youtube"):
    q = query.strip()
    if q.startswith("http"):
        if is_blocked(q):
            raise ValueError("هذا الموقع غير مسموح به في هذا البوت.")
        return q
    if source == "soundcloud":
        return f"scsearch1:{q}"
    return f"ytsearch1:{q}"


def format_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def get_elapsed(guild_id):
    if guild_id not in start_time:
        return 0
    now = time.monotonic()
    paused_extra = (now - paused_at[guild_id]) if guild_id in paused_at else 0
    elapsed = now - start_time[guild_id] - paused_total.get(guild_id, 0) - paused_extra
    return max(0, elapsed)


def mark_paused(guild_id):
    paused_at[guild_id] = time.monotonic()


def mark_resumed(guild_id):
    if guild_id in paused_at:
        paused_total[guild_id] = paused_total.get(guild_id, 0) + (time.monotonic() - paused_at[guild_id])
        del paused_at[guild_id]


def fetch_stream(query, source="youtube"):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(resolve(query, source), download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info["url"], info["title"], info.get("duration"), info.get("thumbnail")


def fetch_with_fallback(query, source):
    """Returns (url, title, duration, thumbnail, used_source). YouTube failures fall back to SoundCloud."""
    try:
        url, title, duration, thumb = fetch_stream(query, source)
        return url, title, duration, thumb, source
    except Exception as first_error:
        if source != "youtube":
            raise
        is_link = query.strip().lower().startswith("http")
        if is_link:
            time.sleep(3)
            try:
                url, title, duration, thumb = fetch_stream(query, "youtube")
                return url, title, duration, thumb, "youtube"
            except Exception:
                raise first_error
        url, title, duration, thumb = fetch_stream(query, "soundcloud")
        return url, title, duration, thumb, "soundcloud"


def make_audio_source(url, start_seconds=0, volume=0.5):
    """Build a PCM audio source, optionally seeking to start_seconds into the stream."""
    before = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        before = f"-ss {int(start_seconds)} " + before
    opts = {**FFMPEG_OPTS, "before_options": before}
    return discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **opts), volume=volume)


async def youtube_suggest(current: str):
    """Fast search suggestions for the /play autocomplete box (needs YOUTUBE_API_KEY)."""
    if not current or not YOUTUBE_API_KEY:
        return []
    params = {
        "part": "snippet",
        "q": current,
        "key": YOUTUBE_API_KEY,
        "type": "video",
        "maxResults": 5,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    results = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not vid:
            continue
        label = f'{snippet.get("title", "")} — {snippet.get("channelTitle", "")}'
        results.append((label[:100], f"https://www.youtube.com/watch?v={vid}"))
    return results


async def delete_panel(guild_id):
    old = panels.pop(guild_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass


async def stop_all(guild):
    gid = guild.id
    queues[gid] = []
    now_playing.pop(gid, None)
    now_source.pop(gid, None)
    now_duration.pop(gid, None)
    current_query.pop(gid, None)
    start_time.pop(gid, None)
    paused_at.pop(gid, None)
    paused_total.pop(gid, None)
    now_url.pop(gid, None)
    now_thumbnail.pop(gid, None)
    now_meta.pop(gid, None)
    seeking_flags.discard(gid)
    loop_mode[gid] = False
    await delete_panel(gid)
    if guild.voice_client:
        await guild.voice_client.disconnect()


def build_embed(guild_id):
    title = now_playing.get(guild_id, "—")
    vol = int(volumes.get(guild_id, 0.5) * 100)
    q = queues.get(guild_id, [])
    loop_on = loop_mode.get(guild_id, False)
    elapsed = format_duration(get_elapsed(guild_id))
    total = format_duration(now_duration.get(guild_id))
    embed = discord.Embed(
        title="🦖 Spinosaurus يزأر الآن",
        description=f"**{title}**",
        color=0x2E5339,  # dark jungle green
    )
    embed.add_field(name="🦴 الوقت", value=f"{elapsed} / {total}", inline=True)
    embed.add_field(name="🌿 الصوت", value=f"{vol}%", inline=True)
    embed.add_field(name="🥚 في الطابور", value=str(len(q)), inline=True)
    embed.add_field(name="🧬 التكرار", value="مفعّل ✅" if loop_on else "متوقف", inline=True)
    embed.set_footer(text=f"🏝️ {now_source.get(guild_id, 'YouTube')} • من أعماق النهر")
    thumb = now_thumbnail.get(guild_id)
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed


class RemoveSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        q = queues.get(guild_id, [])
        options = [
            discord.SelectOption(label=f"{i+1}. {item[0][:90]}", value=str(i))
            for i, item in enumerate(q[:25])
        ]
        super().__init__(placeholder="اختر الأغنية اللي تبغى تشيلها من الطابور", options=options)

    async def callback(self, interaction: discord.Interaction):
        gid = self.guild_id
        idx = int(self.values[0])
        q = queues.get(gid, [])
        if idx >= len(q):
            return await interaction.response.send_message("الطابور تغيّر، جرّب تاني.", ephemeral=True)
        removed = q.pop(idx)
        await interaction.response.send_message(f"🗑️ تم حذف: {removed[0][:80]}", ephemeral=True)
        if gid in panels:
            try:
                await panels[gid].edit(embed=build_embed(gid))
            except Exception:
                pass


class RemoveView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=60)
        self.add_item(RemoveSelect(guild_id))


class VolumeModal(discord.ui.Modal, title="ضبط الصوت"):
    value = discord.ui.TextInput(
        label="النسبة المئوية (10 إلى 150)",
        placeholder="مثال: 70",
        max_length=3,
    )

    def __init__(self, guild_id, panel_view):
        super().__init__()
        self.guild_id = guild_id
        self.panel_view = panel_view

    async def on_submit(self, interaction: discord.Interaction):
        gid = self.guild_id
        try:
            num = int(str(self.value).strip())
        except ValueError:
            return await interaction.response.send_message("لازم تكتب رقم صحيح.", ephemeral=True)
        vol = max(10, min(150, num)) / 100
        volumes[gid] = vol
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = vol
        if gid in panels:
            try:
                await panels[gid].edit(embed=build_embed(gid), view=self.panel_view)
            except Exception:
                pass
        await interaction.response.send_message(f"🔊 الصوت: {int(vol * 100)}%", ephemeral=True)


class ControlPanel(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.loop_button.style = (
            discord.ButtonStyle.success if loop_mode.get(guild_id) else discord.ButtonStyle.secondary
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client if interaction.guild else None
        user_vc = getattr(interaction.user, "voice", None)
        if not vc or not user_vc or user_vc.channel != vc.channel:
            await interaction.response.send_message(
                "لازم تكون معي في نفس الروم الصوتي.", ephemeral=True
            )
            return False
        return True

    # Row 0
    @discord.ui.button(emoji="⏯️", label="إيقاف/تشغيل", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            mark_resumed(gid)
            msg = "▶️ تم الاستئناف"
        elif vc.is_playing():
            vc.pause()
            mark_paused(gid)
            msg = "⏸️ إيقاف مؤقت"
        else:
            msg = "ما فيه شيء يشتغل."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="تخطي", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            loop_mode[gid] = False
            vc.stop()
            await interaction.response.send_message("⏭️ تم التخطي", ephemeral=True)
        else:
            await interaction.response.send_message("ما فيه شيء يشتغل.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="إيقاف", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏹️ تم الإيقاف", ephemeral=True)
        await stop_all(interaction.guild)

    @discord.ui.button(emoji="🦖", label="الحالية", style=discord.ButtonStyle.secondary, row=0)
    async def now_playing_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        if gid not in now_playing:
            return await interaction.response.send_message("ما فيه أغنية شغالة.", ephemeral=True)
        await interaction.response.send_message(embed=build_embed(gid), ephemeral=True)

    # Row 1: seek
    @discord.ui.button(emoji="⏪", label="10 ثواني", style=discord.ButtonStyle.secondary, row=1)
    async def seek_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.do_seek(interaction, -10)

    @discord.ui.button(emoji="⏩", label="10 ثواني", style=discord.ButtonStyle.secondary, row=1)
    async def seek_forward(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.do_seek(interaction, +10)

    async def do_seek(self, interaction: discord.Interaction, delta: int):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        if not vc or gid not in now_url:
            return await interaction.response.send_message("ما فيه أغنية شغالة.", ephemeral=True)

        new_pos = get_elapsed(gid) + delta
        total = now_duration.get(gid)
        new_pos = max(0, new_pos)
        if total:
            new_pos = min(new_pos, max(total - 2, 0))

        seeking_flags.add(gid)
        vc.stop()
        source = make_audio_source(now_url[gid], start_seconds=new_pos, volume=volumes.get(gid, 0.5))
        vc.play(source, after=make_after(gid))
        start_time[gid] = time.monotonic() - new_pos
        paused_total[gid] = 0
        paused_at.pop(gid, None)

        await interaction.response.edit_message(embed=build_embed(gid), view=self)

    # Row 2
    @discord.ui.button(emoji="🔉", label="أخفض", style=discord.ButtonStyle.secondary, row=2)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, -0.1)

    @discord.ui.button(emoji="🔊", label="أعلى", style=discord.ButtonStyle.secondary, row=2)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, +0.1)

    @discord.ui.button(emoji="🎚️", label="رقم", style=discord.ButtonStyle.secondary, row=2)
    async def vol_input(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        await interaction.response.send_modal(VolumeModal(gid, self))

    @discord.ui.button(emoji="🧬", label="تكرار", style=discord.ButtonStyle.secondary, row=2)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        new_state = not loop_mode.get(gid, False)
        loop_mode[gid] = new_state
        button.style = discord.ButtonStyle.success if new_state else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=build_embed(gid), view=self)

    @discord.ui.button(emoji="🥚", label="الطابور", style=discord.ButtonStyle.secondary, row=2)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid, [])
        if not q:
            return await interaction.response.send_message("الطابور فاضي.", ephemeral=True)
        lines = [f"{i}. {item[0][:60]}" for i, item in enumerate(q[:10], 1)]
        if len(q) > 10:
            lines.append(f"... و{len(q) - 10} أخرى")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # Row 3
    @discord.ui.button(emoji="🔀", label="خلط", style=discord.ButtonStyle.secondary, row=3)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid, [])
        if len(q) < 2:
            return await interaction.response.send_message("محتاج أغنيتين على الأقل بالطابور.", ephemeral=True)
        random.shuffle(q)
        await interaction.response.send_message("🔀 تم خلط الطابور", ephemeral=True)
        if gid in panels:
            try:
                await panels[gid].edit(embed=build_embed(gid))
            except Exception:
                pass

    @discord.ui.button(emoji="🌋", label="تفريغ", style=discord.ButtonStyle.secondary, row=3)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        queues[gid] = []
        await interaction.response.send_message("🧹 تم تفريغ الطابور (الأغنية الحالية مستمرة)", ephemeral=True)
        if gid in panels:
            try:
                await panels[gid].edit(embed=build_embed(gid))
            except Exception:
                pass

    @discord.ui.button(emoji="🦴", label="حذف", style=discord.ButtonStyle.secondary, row=3)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid, [])
        if not q:
            return await interaction.response.send_message("الطابور فاضي.", ephemeral=True)
        await interaction.response.send_message(
            "اختر الأغنية اللي تبغى تحذفها:", view=RemoveView(gid), ephemeral=True
        )

    async def change_volume(self, interaction: discord.Interaction, delta):
        gid = interaction.guild.id
        vol = round(min(1.5, max(0.1, volumes.get(gid, 0.5) + delta)), 2)
        volumes[gid] = vol
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = vol
        await interaction.response.edit_message(embed=build_embed(gid), view=self)


def make_after(gid):
    """Returns an `after` callback for vc.play(). Shared by normal playback and seeking,
    so seeking (which calls vc.stop()+vc.play() again) doesn't skip to the next track."""
    def after(err):
        if gid in seeking_flags:
            seeking_flags.discard(gid)
            return
        meta = now_meta.get(gid)
        if not meta:
            return
        if loop_mode.get(gid):
            queues.setdefault(gid, []).insert(0, (meta["query"], meta["src"]))
        asyncio.run_coroutine_threadsafe(play_next(meta["guild"], meta["channel"]), bot.loop)
    return after


async def play_next(guild, channel):
    gid = guild.id
    vc = guild.voice_client
    if not vc:
        return
    q = queues.get(gid, [])
    if not q:
        now_playing.pop(gid, None)
        await delete_panel(gid)
        return
    if vc.is_playing() or vc.is_paused():
        return

    query, src = q.pop(0)
    try:
        url, title, duration, thumb, src = await bot.loop.run_in_executor(
            None, fetch_with_fallback, query, src
        )
    except Exception as e:
        await channel.send(f"❌ فشل: {str(e)[:300]}")
        return await play_next(guild, channel)

    audio_source = make_audio_source(url, start_seconds=0, volume=volumes.get(gid, 0.5))
    vc.play(audio_source, after=make_after(gid))
    now_playing[gid] = title
    now_source[gid] = "SoundCloud" if src == "soundcloud" else "YouTube"
    now_duration[gid] = duration
    now_url[gid] = url
    now_thumbnail[gid] = thumb
    now_meta[gid] = {"guild": guild, "channel": channel, "query": query, "src": src}
    current_query[gid] = (query, src)
    start_time[gid] = time.monotonic()
    paused_total[gid] = 0
    paused_at.pop(gid, None)

    play_counts.setdefault(gid, Counter())[title] += 1

    await delete_panel(gid)
    panels[gid] = await channel.send(embed=build_embed(gid), view=ControlPanel(gid))


def guess_source(query: str) -> str:
    q = query.strip().lower()
    if q.startswith("http") and "soundcloud.com" in q:
        return "soundcloud"
    return "youtube"


async def queue_and_play(ctx, query, source):
    if not ctx.author.voice:
        return await ctx.send("ادخل روم صوتي أول.")
    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()
    queues.setdefault(ctx.guild.id, []).append((query, source))
    vc = ctx.voice_client
    if vc.is_playing() or vc.is_paused():
        await ctx.send("➕ انضافت للطابور")
        if ctx.guild.id in panels:
            try:
                await panels[ctx.guild.id].edit(embed=build_embed(ctx.guild.id))
            except Exception:
                pass
    else:
        await play_next(ctx.guild, ctx.channel)


@bot.command(name="music", aliases=["play", "p", "yt"])
async def music(ctx, *, query):
    """يشغّل من يوتيوب افتراضياً، أو من رابط ساوند كلاود لو حطيته."""
    await queue_and_play(ctx, query, guess_source(query))


@bot.command(name="sc", aliases=["soundcloud"])
async def sc(ctx, *, query):
    """يجبر البحث على ساوند كلاود فقط."""
    await queue_and_play(ctx, query, "soundcloud")


@bot.command(name="loop")
async def loop_cmd(ctx):
    gid = ctx.guild.id
    new_state = not loop_mode.get(gid, False)
    loop_mode[gid] = new_state
    await ctx.send("🔂 التكرار: مفعّل ✅" if new_state else "🔂 التكرار: متوقف")
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


@bot.command(name="shuffle")
async def shuffle_cmd(ctx):
    gid = ctx.guild.id
    q = queues.get(gid, [])
    if len(q) < 2:
        return await ctx.send("محتاج أغنيتين على الأقل بالطابور.")
    random.shuffle(q)
    await ctx.send("🔀 تم خلط الطابور")
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


@bot.command(name="clear")
async def clear_cmd(ctx):
    gid = ctx.guild.id
    queues[gid] = []
    await ctx.send("🧹 تم تفريغ الطابور (الأغنية الحالية مستمرة)")
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


@bot.command(name="remove")
async def remove_cmd(ctx, index: int):
    gid = ctx.guild.id
    q = queues.get(gid, [])
    if index < 1 or index > len(q):
        return await ctx.send(f"رقم غير صحيح. الطابور فيه {len(q)} أغنية.")
    removed = q.pop(index - 1)
    await ctx.send(f"🗑️ تم حذف: {removed[0][:80]}")
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx):
    gid = ctx.guild.id
    if gid not in now_playing:
        return await ctx.send("ما فيه أغنية شغالة.")
    await ctx.send(embed=build_embed(gid))


@bot.command(name="top")
async def top_cmd(ctx):
    gid = ctx.guild.id
    counts = play_counts.get(gid)
    if not counts:
        return await ctx.send("ما فيه إحصائيات بعد (تصفّر كل ما يعاد تشغيل البوت).")
    top5 = counts.most_common(5)
    lines = [f"{i}. {title[:60]} — {n} مرة" for i, (title, n) in enumerate(top5, 1)]
    embed = discord.Embed(
        title="🏆 أكثر 5 أغاني تشغيل", description="\n".join(lines), color=0xFFD700
    )
    await ctx.send(embed=embed)


@bot.command(name="volume", aliases=["vol"])
async def volume_cmd(ctx, value: int):
    gid = ctx.guild.id
    vol = max(10, min(150, value)) / 100
    volumes[gid] = vol
    vc = ctx.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol
    await ctx.send(f"🔊 الصوت: {int(vol * 100)}%")
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


async def seek_command(ctx, delta):
    gid = ctx.guild.id
    vc = ctx.voice_client
    if not vc or gid not in now_url:
        return await ctx.send("ما فيه أغنية شغالة.")
    new_pos = get_elapsed(gid) + delta
    total = now_duration.get(gid)
    new_pos = max(0, new_pos)
    if total:
        new_pos = min(new_pos, max(total - 2, 0))
    seeking_flags.add(gid)
    vc.stop()
    source = make_audio_source(now_url[gid], start_seconds=new_pos, volume=volumes.get(gid, 0.5))
    vc.play(source, after=make_after(gid))
    start_time[gid] = time.monotonic() - new_pos
    paused_total[gid] = 0
    paused_at.pop(gid, None)
    if gid in panels:
        try:
            await panels[gid].edit(embed=build_embed(gid))
        except Exception:
            pass


@bot.command(name="forward", aliases=["fwd"])
async def forward_cmd(ctx):
    await seek_command(ctx, +10)


@bot.command(name="back", aliases=["rewind"])
async def back_cmd(ctx):
    await seek_command(ctx, -10)


@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        loop_mode[ctx.guild.id] = False
        ctx.voice_client.stop()
        await ctx.send("⏭️ تم التخطي")


@bot.command()
async def pause(ctx):
    if ctx.voice_client:
        ctx.voice_client.pause()
        mark_paused(ctx.guild.id)


@bot.command()
async def resume(ctx):
    if ctx.voice_client:
        ctx.voice_client.resume()
        mark_resumed(ctx.guild.id)


@bot.command()
async def stop(ctx):
    if ctx.voice_client:
        await stop_all(ctx.guild)
        await ctx.send("⏹️ تم الإيقاف")


# ---------- Slash command with live search suggestions (/play) ----------

class CtxShim:
    """Minimal adapter so queue_and_play() can work from a slash-command Interaction."""
    def __init__(self, interaction: discord.Interaction):
        self.guild = interaction.guild
        self.author = interaction.user
        self.channel = interaction.channel
        self._interaction = interaction

    @property
    def voice_client(self):
        return self.guild.voice_client

    async def send(self, *args, **kwargs):
        return await self._interaction.followup.send(*args, **kwargs)


async def play_autocomplete(interaction: discord.Interaction, current: str):
    suggestions = await youtube_suggest(current)
    return [app_commands.Choice(name=label, value=url) for label, url in suggestions]


@bot.tree.command(name="play", description="شغّل أغنية من يوتيوب أو ساوند كلاود")
@app_commands.describe(query="اسم الأغنية أو الرابط")
@app_commands.autocomplete(query=play_autocomplete)
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    shim = CtxShim(interaction)
    await queue_and_play(shim, query, guess_source(query))


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print("Slash command sync failed:", e)


load_opus()
bot.run(TOKEN)
