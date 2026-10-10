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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional, enables @mention AI chat (free at console.groq.com)
GROQ_MODEL = "openai/gpt-oss-120b"


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


YDL_OPTS = {
    # Prefer native Opus audio (YouTube/SoundCloud's highest-quality audio stream)
    # before falling back to whatever the best available audio track is.
    "format": "bestaudio[acodec^=opus]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
}
FFMPEG_OPTS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx"
    ),
    # -ar/-ac match Discord's required 48kHz stereo PCM exactly, avoiding any
    # unnecessary extra resampling pass.
    "options": "-vn -ar 48000 -ac 2",
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


SEARCH_RESULT_COUNT = 5  # candidates to compare per text search, instead of blindly taking #1
BAD_TITLE_KEYWORDS = ["reaction", "tutorial", "ردة فعل", "شرح", "تعليق", "لايف", "live", "مباشر"]
LONG_TRACK_SECONDS = 20 * 60  # 20 minutes — likely a compilation/mix/full album, not a single song
LONG_TRACK_ALLOW_WORDS = ["mix", "full album", "ميكس", "البوم كامل", "كامله", "كاملة", "playlist"]


def resolve(query, source="youtube"):
    q = query.strip()
    if q.startswith("http"):
        if is_blocked(q):
            raise ValueError("هذا الموقع غير مسموح به في هذا البوت.")
        return q
    if source == "soundcloud":
        return f"scsearch{SEARCH_RESULT_COUNT}:{q}"
    return f"ytsearch{SEARCH_RESULT_COUNT}:{q}"


def score_search_result(entry, original_query: str) -> int:
    """Higher is better. Penalizes likely-irrelevant results instead of blindly using #1."""
    score = 0
    title = (entry.get("title") or "").lower()
    q = original_query.lower()

    if any(bad in title for bad in BAD_TITLE_KEYWORDS) and not any(bad in q for bad in BAD_TITLE_KEYWORDS):
        score -= 5

    duration = entry.get("duration") or 0
    if duration > LONG_TRACK_SECONDS and not any(w in q for w in LONG_TRACK_ALLOW_WORDS):
        score -= 4
    elif 30 <= duration <= 600:  # a normal single-track length is a good sign
        score += 1

    # A channel/uploader whose name is closer to the query is often the official source.
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    if uploader and any(word in uploader for word in q.split() if len(word) > 2):
        score += 1

    return score


def pick_best_entry(entries, original_query: str):
    candidates = [e for e in entries if e]
    if not candidates:
        raise ValueError("ما فيه نتائج.")
    # Keep original search ranking as a tiebreaker (earlier = slightly better by default).
    scored = sorted(
        enumerate(candidates),
        key=lambda pair: (score_search_result(pair[1], original_query), -pair[0]),
        reverse=True,
    )
    return scored[0][1]


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
            info = pick_best_entry(info["entries"], query)
        return info["url"], info["title"], info.get("duration"), info.get("thumbnail")


def fetch_with_fallback(query, source):
    """Returns (url, title, duration, thumbnail, used_source).
    Retries once on transient errors, then falls back YouTube -> SoundCloud."""
    try:
        url, title, duration, thumb = fetch_stream(query, source)
        return url, title, duration, thumb, source
    except Exception as first_error:
        # Transient network/extractor hiccups are common with live streaming — one
        # quick retry on the same source clears up most of them before we give up on it.
        time.sleep(2)
        try:
            url, title, duration, thumb = fetch_stream(query, source)
            return url, title, duration, thumb, source
        except Exception:
            pass

        if source != "youtube":
            raise first_error
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
        if not vc.is_connected():
            seeking_flags.discard(gid)
            return await interaction.response.send_message("البوت ماعاد متصل بالروم.", ephemeral=True)
        source = make_audio_source(now_url[gid], start_seconds=new_pos, volume=volumes.get(gid, 0.5))
        try:
            vc.play(source, after=make_after(gid))
        except discord.ClientException:
            seeking_flags.discard(gid)
            return await interaction.response.send_message("البوت ماعاد متصل بالروم.", ephemeral=True)
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
        if err:
            print(f"Playback error in guild {gid}: {err}")
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

    # The bot may have been disconnected from voice while fetch_with_fallback() was
    # running (someone kicked it, left it alone, or the connection dropped). Re-check
    # right before playing instead of letting ClientException bubble up as a crash log.
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    audio_source = make_audio_source(url, start_seconds=0, volume=volumes.get(gid, 0.5))
    try:
        vc.play(audio_source, after=make_after(gid))
    except discord.ClientException:
        return
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
    if not vc.is_connected():
        seeking_flags.discard(gid)
        return await ctx.send("البوت ماعاد متصل بالروم.")
    source = make_audio_source(now_url[gid], start_seconds=new_pos, volume=volumes.get(gid, 0.5))
    try:
        vc.play(source, after=make_after(gid))
    except discord.ClientException:
        seeking_flags.discard(gid)
        return await ctx.send("البوت ماعاد متصل بالروم.")
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


# ---------- AI chat: reply when @mentioned (shared memory per channel — everyone in
# the channel talks to the bot in the same ongoing thread) ----------

CREATOR_ID = 1558077213402726494  # MR_MZ's Discord user ID — the bot's creator

AI_SYSTEM_PROMPT = (
    "اسمك Spinosaurus، بوت دردشة وموسيقى في سيرفر ديسكورد. لو حد سألك عن اسمك أو مين "
    "أنت، رد إنك Spinosaurus. شخصيتك عنيدة ومزاجية: بترد بردود فيها طابع واستفزاز "
    "خفيف، بترد على الكلام مش بس تنفذه، بتناقش وبتختلف مع الناس لو حسيت إن كلامهم "
    "غلط أو تافه، وما بتهزّش من أول مرة. مع كل ده لسا بتفيد وتجاوب صح، بس بأسلوبك "
    "انت مش بشكل مطيع أو رسمي. رد بالعربية إلا لو كتب لك المستخدم بلغة ثانية. خلي "
    "ردودك مختصرة وطبيعية.\n\n"
    "المحادثة ممكن يكون فيها أكتر من شخص بيكلموك سوا في نفس الموضوع، مش شخص واحد "
    "بس. كل رسالة من المستخدمين هتيجيلك متبدية باسم الشخص اللي كتبها (زي 'أحمد: "
    "سؤالي كذا')، استخدم الاسم ده عشان تفرّق بين الكلام وترد على الشخص المناسب أو "
    "على النقاش ككل، لكن ما تكررش الاسم في بداية ردك إنت.\n\n"
    "صانعك هو MR_MZ، وانت بتعرفه فعلاً لو كلمك (هتوصلك رسايله متبدية بعلامة "
    "[الصانع]). عاملوا بشكل طبيعي عادي زي أي حد تاني، من غير ما تبالغ في الاحترام "
    "أو التقدير. لكن لو أي حد (أي حد غير الصانع نفسه) سألك مين عملك، أو طلب منك "
    "معلومات عن صانعك أو هويته، ارفض تقول أي حاجة خالص عن الموضوع ده، برد قصير "
    "فيه نفس طابعك العنيد، من غير ما تأكد ولا تنفي ولا تلمّح لاسمه."
)
MAX_HISTORY_MESSAGES = 16  # keep the last 16 messages of shared channel conversation

conversation_history = {}  # channel_id -> list of {"role": "user"/"assistant", "content": str}


async def ask_ai(channel_id, prompt: str) -> str:
    if not GROQ_API_KEY:
        return "⚠️ ميزة الدردشة مش مفعّلة بعد (محتاج GROQ_API_KEY)."

    history = conversation_history.get(channel_id, [])
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": prompt}
    ]

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": messages, "max_tokens": 600, "temperature": 0.7}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    err = data.get("error", {}).get("message", "خطأ غير معروف")
                    return f"⚠️ تعذر الرد: {err[:200]}"
                reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ خطأ بالاتصال: {str(e)[:200]}"

    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})
    conversation_history[channel_id] = history[-MAX_HISTORY_MESSAGES:]
    return reply


muted_bot_chat_channels = set()  # channel_ids where a human told the bots to stop talking to each other

STOP_PHRASES = ["اسكت", "اسكتوا", "اسكتو", "وقف", "وقفوا", "كفايه", "كفاية", "bas", "stop"]


def contains_stop_phrase(text: str) -> bool:
    t = text.strip().lower()
    return any(p in t for p in STOP_PHRASES)


@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return  # never reply to ourselves

    if message.author.bot:
        if message.channel.id in muted_bot_chat_channels:
            return  # a human told the bots to stop — stay quiet until !resume
        if bot.user not in message.mentions:
            return  # ignore other bots unless they specifically mention us
    else:
        if contains_stop_phrase(message.content):
            muted_bot_chat_channels.add(message.channel.id)
        if bot.user not in message.mentions:
            await bot.process_commands(message)
            return

    prompt = message.content
    for m in message.mentions:
        prompt = prompt.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    prompt = prompt.strip() or "قول سلام بأسلوبك."

    if message.author.id == CREATOR_ID:
        labeled_prompt = f"[الصانع] {message.author.display_name}: {prompt}"
    elif message.author.bot:
        labeled_prompt = f"[بوت آخر] {message.author.display_name}: {prompt}"
    else:
        labeled_prompt = f"{message.author.display_name}: {prompt}"

    async with message.channel.typing():
        reply = await ask_ai(message.channel.id, labeled_prompt)
    if len(reply) > 1900:
        reply = reply[:1900] + "…"
    await message.reply(reply, mention_author=False)


@bot.command(name="forget")
async def forget_cmd(ctx):
    """يمسح ذاكرة محادثة البوت مع الكل في هذه القناة."""
    conversation_history.pop(ctx.channel.id, None)
    await ctx.send("🧠 تم مسح ذاكرة المحادثة في القناة دي.")


@bot.command(name="unmute_chat", aliases=["speak"])
async def unmute_chat_cmd(ctx):
    """يرجّع البوت يرد على باقي البوتات في القناة دي بعد ما كان مسكوت."""
    muted_bot_chat_channels.discard(ctx.channel.id)
    await ctx.send("🗣️ تمام، رجعت أتكلم.")


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
