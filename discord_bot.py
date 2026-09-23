import asyncio
import ctypes.util
import json
import logging
import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord-music-bot")


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=commands.DefaultHelpCommand(
        no_category="الأوامر",
        dm_help=True,
    ),
)

YTDL_OPTIONS = {
    "extractor_args": {"youtube": {"player_client": ["ios", "android"]}},
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "format": "bestaudio/best"
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
PANEL_ART_PATH = "attached_assets/generated_images/spinosaurus_panel_banner.png"
PANEL_ART_FILENAME = "spinosaurus_panel_banner.png"
VOICE_STATE_PATH = "voice_state.json"


@dataclass
class Track:
    query: str
    title: str = "مقطع جديد"
    requested_by: str = ""
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    webpage_url: Optional[str] = None


@dataclass
class GuildPlayer:
    """Playback state for one Discord server."""

    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    source: Optional[discord.PCMVolumeTransformer] = None
    volume: float = 0.5
    repeat: bool = False
    voice_channel_id: Optional[int] = None
    keep_in_voice: bool = False
    voice_reconnect_task: Optional[asyncio.Task[None]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


players: dict[int, GuildPlayer] = {}
voice_state_restored = False


def load_opus_library() -> None:
    """Load Opus, including from the Nix path used by FFmpeg."""
    if discord.opus.is_loaded():
        return

    candidates = [
        os.getenv("OPUS_LIBRARY", ""),
        ctypes.util.find_library("opus") or "",
    ]

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            ldd_output = subprocess.run(
                ["ldd", ffmpeg],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            candidates.extend(
                re.findall(r"libopus\.so[^ ]* => (\S+)", ldd_output)
            )
        except (OSError, subprocess.SubprocessError):
            pass

    candidates.extend(["libopus.so.0", "libopus.so"])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            discord.opus.load_opus(candidate)
        except OSError:
            continue
        if discord.opus.is_loaded():
            logger.info("Loaded Opus library from %s", candidate)
            return

#    raise RuntimeError(
 #       "Opus library could not be loaded. Install the libopus system dependency."
  #  )

def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def load_voice_state() -> None:
    """Restore voice channels that should be rejoined after a process restart."""
    if not os.path.exists(VOICE_STATE_PATH):
        return
    try:
        with open(VOICE_STATE_PATH, encoding="utf-8") as state_file:
            saved_state = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not load saved voice state")
        return

    if not isinstance(saved_state, dict):
        return
    for guild_id, channel_id in saved_state.items():
        try:
            player = get_player(int(guild_id))
            player.voice_channel_id = int(channel_id)
            player.keep_in_voice = True
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid saved voice state: %r -> %r", guild_id, channel_id)


def save_voice_state() -> None:
    """Persist only the requested voice channels; playback queue stays in memory."""
    saved_state = {
        str(guild_id): player.voice_channel_id
        for guild_id, player in players.items()
        if player.keep_in_voice and player.voice_channel_id
    }
    temporary_path = f"{VOICE_STATE_PATH}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(saved_state, state_file)
        os.replace(temporary_path, VOICE_STATE_PATH)
    except OSError:
        logger.exception("Could not save voice state")


def panel_embed(guild_id: int) -> discord.Embed:
    player = get_player(guild_id)
    track = player.current
    current = track.title if track else "لا يوجد مقطع يعمل"
    queued = len(player.queue)
    repeat_state = "مفعّل" if player.repeat else "متوقف"

    embed = discord.Embed(
        title="🦖 SPINOSAURUS // MUSIC EXPEDITION",
        description=f"🎵 **الآن يعمل**\n{current}",
        color=0xB7791F,
    )
    embed.set_image(url=f"attachment://{PANEL_ART_FILENAME}")
    if track and track.webpage_url:
        embed.url = track.webpage_url
    if track and track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    details = [
        f"👤 **الطلب بواسطة:** {track.requested_by or 'عضو السيرفر'}"
        if track
        else "لا يوجد مقطع في التشغيل",
        f"🔊 **الصوت:** {round(player.volume * 100)}%",
        f"📜 **في الانتظار:** {queued} مقطع",
        f"🔁 **التكرار:** {repeat_state}",
    ]
    if track and track.duration:
        details.insert(1, f"⏱️ **المدة:** {format_duration(track.duration)}")
    embed.add_field(name="Spinosaurus Music", value="\n".join(details), inline=False)
    embed.set_footer(text="PREHISTORIC RADIO • استخدم !music لإضافة أغنية")
    return embed


def format_duration(seconds: int) -> str:
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


async def extract_audio(
    search: str,
) -> tuple[discord.AudioSource, str, Optional[str], Optional[int], Optional[str]]:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(search, download=False),
    )

    if not data:
        raise RuntimeError("لم يتم العثور على المقطع.")

    if "entries" in data:
        entries = data["entries"]
        if not entries:
            raise RuntimeError("لم يتم العثور على المقطع.")
        data = entries[0]

    stream_url = data.get("url")
    title = data.get("title") or "مقطع صوتي"
    if not stream_url:
        raise RuntimeError("تعذر الحصول على رابط الصوت.")

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
        volume=0.5,
    )
    return (
        source,
        title,
        data.get("thumbnail"),
        data.get("duration"),
        data.get("webpage_url"),
    )


async def ensure_voice(
    ctx: commands.Context,
) -> Optional[discord.VoiceClient]:
    if not ctx.guild:
        await ctx.send("هذا الأمر يعمل داخل السيرفر فقط.")
        return None

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("ادخل روم صوتي أولًا ثم استخدم الأمر.")
        return None

    channel = ctx.author.voice.channel
    voice_client = ctx.voice_client
    player = get_player(ctx.guild.id)

    try:
        if voice_client is None:
            voice_client = await channel.connect(reconnect=True)
        elif voice_client.channel != channel:
            await voice_client.move_to(channel)
    except (discord.ClientException, asyncio.TimeoutError):
        logger.exception("Could not connect to voice channel")
        await ctx.send("تعذر دخول الروم. تأكد من صلاحيات Connect وSpeak.")
        return None

    player.voice_channel_id = channel.id
    player.keep_in_voice = True
    save_voice_state()
    return voice_client


def schedule_voice_reconnect(guild_id: int) -> None:
    player = get_player(guild_id)
    task = player.voice_reconnect_task
    if task and not task.done():
        return
    player.voice_reconnect_task = asyncio.create_task(
        maintain_voice_connection(guild_id)
    )


async def maintain_voice_connection(guild_id: int) -> None:
    """Keep a requested voice connection alive after an unexpected disconnect."""
    player = get_player(guild_id)

    while player.keep_in_voice:
        await asyncio.sleep(5)
        if not player.keep_in_voice or not player.voice_channel_id:
            return

        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(player.voice_channel_id) if guild else None
        if not guild or not isinstance(channel, discord.VoiceChannel):
            logger.warning("Voice channel %s is no longer available", player.voice_channel_id)
            return

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            return

        try:
            if voice_client:
                await voice_client.disconnect(force=True)
            voice_client = await channel.connect(reconnect=True)

            # Put an interrupted track back at the front so it can resume.
            if player.current:
                player.queue.insert(0, player.current)
                player.current = None
                player.source = None
            await start_next(guild_id, voice_client)
            logger.info("Reconnected to voice channel %s", channel.id)
            return
        except (discord.ClientException, asyncio.TimeoutError):
            logger.exception("Could not reconnect to voice channel %s", channel.id)


async def disconnect_player(
    guild_id: int,
    voice_client: Optional[discord.VoiceClient],
) -> None:
    """Stop playback and disable automatic voice reconnection."""
    player = get_player(guild_id)
    player.keep_in_voice = False
    player.voice_channel_id = None
    save_voice_state()
    reconnect_task = player.voice_reconnect_task
    if reconnect_task and not reconnect_task.done():
        reconnect_task.cancel()
    player.voice_reconnect_task = None
    player.queue.clear()
    player.current = None
    player.source = None

    if voice_client:
        voice_client.stop()
        await voice_client.disconnect()


async def start_next(
    guild_id: int,
    voice_client: discord.VoiceClient,
    announce_channel: Optional[discord.abc.Messageable] = None,
) -> bool:
    player = get_player(guild_id)

    async with player.lock:
        if voice_client.is_playing() or voice_client.is_paused():
            return False

        if player.current and player.repeat:
            track = player.current
        elif player.queue:
            track = player.queue.pop(0)
            player.current = track
        else:
            player.current = None
            player.source = None
            return False

        while True:
            try:
                async with (
                    announce_channel.typing()
                    if announce_channel
                    else _null_async_context()
                ):
                    (
                        source,
                        title,
                        thumbnail,
                        duration,
                        webpage_url,
                    ) = await extract_audio(track.query)
            except Exception:
                logger.exception("Could not extract audio for %r", track.query)
                player.current = None
                player.source = None
                if announce_channel:
                    await announce_channel.send(
                        "تعذر تشغيل هذا المقطع. جرّب رابطًا أو اسمًا مختلفًا."
                    )
                if not player.queue:
                    return False
                track = player.queue.pop(0)
                player.current = track
                continue

            track.title = title
            track.thumbnail = thumbnail
            track.duration = duration
            track.webpage_url = webpage_url
            source.volume = player.volume
            player.source = source

            def after_playback(error: Optional[Exception]) -> None:
                if error:
                    logger.error("Playback error: %s", error)
                future = asyncio.run_coroutine_threadsafe(
                    start_next(guild_id, voice_client, announce_channel),
                    bot.loop,
                )
                future.add_done_callback(
                    lambda completed: (
                        logger.error(
                            "Could not start queued track: %s",
                            completed.exception(),
                        )
                        if not completed.cancelled() and completed.exception()
                        else None
                    )
                )

            voice_client.play(source, after=after_playback)
            if announce_channel:
                await announce_channel.send(f"شغال الآن: **{title}**")
            return True


class _null_async_context:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class VolumeModal(discord.ui.Modal, title="ضبط مستوى الصوت"):
    volume = discord.ui.TextInput(
        label="مستوى الصوت من 1 إلى 100",
        placeholder="80",
        min_length=1,
        max_length=3,
        required=True,
    )

    def __init__(self, guild_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.volume))
        except ValueError:
            await interaction.response.send_message(
                "اكتب رقمًا صحيحًا من 1 إلى 100.",
                ephemeral=True,
            )
            return

        if not 1 <= value <= 100:
            await interaction.response.send_message(
                "استخدم رقمًا من 1 إلى 100.",
                ephemeral=True,
            )
            return

        player = get_player(self.guild_id)
        player.volume = value / 100
        if player.source:
            player.source.volume = player.volume
        await interaction.response.send_message(
            f"تم ضبط الصوت على {value}%.",
            ephemeral=True,
        )


class MusicPanel(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "هذه اللوحة تخص سيرفرًا آخر.",
                ephemeral=True,
            )
            return False

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice:
            await interaction.response.send_message(
                "ادخل نفس الروم الصوتي أولًا.",
                ephemeral=True,
            )
            return False

        voice_client = interaction.guild.voice_client
        if voice_client and member.voice.channel != voice_client.channel:
            await interaction.response.send_message(
                "يجب أن تكون في نفس روم البوت.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="تشغيل",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def play_info(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "لتشغيل أغنية اكتب:\n`!music اسم الأغنية أو رابط يوتيوب`",
            ephemeral=True,
        )

    @discord.ui.button(
        label="إيقاف مؤقت",
        emoji="⏯️",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def pause_resume(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        voice_client = interaction.guild.voice_client
        if not voice_client:
            await interaction.response.send_message(
                "البوت ليس داخل روم صوتي.",
                ephemeral=True,
            )
            return
        if voice_client.is_playing():
            voice_client.pause()
            button.label = "استئناف"
        elif voice_client.is_paused():
            voice_client.resume()
            button.label = "إيقاف مؤقت"
        else:
            await interaction.response.send_message(
                "لا يوجد مقطع يعمل حاليًا.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=panel_embed(self.guild_id),
            view=self,
        )

    @discord.ui.button(
        label="تخطّي",
        emoji="⏭️",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def skip(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
            await interaction.response.edit_message(
                content="تم التخطي — يتم تشغيل المقطع التالي.",
                embed=panel_embed(self.guild_id),
                view=self,
            )
        else:
            await interaction.response.send_message(
                "لا يوجد مقطع يمكن تخطيه.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="إيقاف",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def stop(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        voice_client = interaction.guild.voice_client
        await disconnect_player(self.guild_id, voice_client)
        await interaction.response.edit_message(
            content="تم الإيقاف ومغادرة الروم.",
            embed=panel_embed(self.guild_id),
            view=self,
        )

    @discord.ui.button(
        label="القائمة",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def show_queue(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        player = get_player(self.guild_id)
        items = [
            f"{index}. {track.title}"
            for index, track in enumerate(player.queue[:10], start=1)
        ]
        queue_text = "\n".join(items) if items else "القائمة فارغة."
        await interaction.response.send_message(queue_text, ephemeral=True)

    @discord.ui.button(
        label="تكرار",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def toggle_repeat(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        player = get_player(self.guild_id)
        player.repeat = not player.repeat
        button.label = "التكرار: تشغيل" if player.repeat else "التكرار: إيقاف"
        await interaction.response.edit_message(
            embed=panel_embed(self.guild_id),
            view=self,
        )

    @discord.ui.button(
        label="خلط",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def shuffle(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        player = get_player(self.guild_id)
        random.shuffle(player.queue)
        await interaction.response.edit_message(
            content="تم خلط قائمة الانتظار.",
            embed=panel_embed(self.guild_id),
            view=self,
        )

    @discord.ui.button(
        label="الصوت",
        emoji="🔊",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def volume(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(VolumeModal(self.guild_id))

    @discord.ui.button(
        label="مساعدة",
        emoji="❔",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def help_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "الأوامر الأساسية:\n"
            "`!music` تشغيل أو إضافة مقطع\n"
            "`!queue` القائمة\n"
            "`!skip` تخطّي\n"
            "`!loop` تكرار\n"
            "`!shuffle` خلط\n"
            "`!volume 1-100` الصوت\n"
            "`!stop` إيقاف ومغادرة",
            ephemeral=True,
        )


@bot.event
async def on_ready() -> None:
    if not bot.user:
        return

    desired_name = os.getenv("BOT_NAME", "Spinosaurus").strip()
    if desired_name and bot.user.name != desired_name:
        try:
            await bot.user.edit(username=desired_name)
            logger.info("Bot username changed to %s", desired_name)
        except discord.HTTPException:
            logger.exception("Could not change bot username")
    await bot.change_presence(
        activity=discord.Game(name="🦖 !panel • Prehistoric Radio"),
    )
    global voice_state_restored
    if not voice_state_restored:
        load_voice_state()
        voice_state_restored = True
        for guild_id, player in players.items():
            if player.keep_in_voice and player.voice_channel_id:
                schedule_voice_reconnect(guild_id)
    logger.info("Bot is online as %s (id=%s)", bot.user, bot.user.id)


@bot.event
async def on_disconnect() -> None:
    logger.warning("Discord gateway disconnected; discord.py will reconnect automatically")


@bot.event
async def on_resumed() -> None:
    logger.info("Discord gateway session resumed")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not bot.user or member.id != bot.user.id or before.channel is None:
        return
    if after.channel is None:
        player = players.get(member.guild.id)
        if player and player.keep_in_voice:
            logger.warning(
                "Bot left voice unexpectedly in guild %s; scheduling reconnect",
                member.guild.id,
            )
            schedule_voice_reconnect(member.guild.id)


@bot.command(
    name="music",
    aliases=["p", "play"],
    help="تشغيل أغنية أو إضافة رابط للقائمة",
)
async def play(ctx: commands.Context, *, search: str) -> None:
    voice_client = await ensure_voice(ctx)
    if voice_client is None or not ctx.guild:
        return

    player = get_player(ctx.guild.id)
    track = Track(
        query=search,
        requested_by=getattr(ctx.author, "display_name", ""),
    )
    was_idle = not voice_client.is_playing() and not voice_client.is_paused()
    player.queue.append(track)

    if was_idle:
        await start_next(ctx.guild.id, voice_client, ctx.channel)
    else:
        await ctx.send("تمت إضافة المقطع إلى قائمة الانتظار.")


@bot.command(name="pause", help="إيقاف مؤقت")
async def pause(ctx: commands.Context) -> None:
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("تم الإيقاف المؤقت.")
    else:
        await ctx.send("لا يوجد مقطع يعمل حاليًا.")


@bot.command(name="resume", help="استئناف التشغيل")
async def resume(ctx: commands.Context) -> None:
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("تم استئناف التشغيل.")
    else:
        await ctx.send("لا يوجد مقطع متوقف مؤقتًا.")


@bot.command(name="skip", help="تخطي المقطع الحالي")
async def skip(ctx: commands.Context) -> None:
    if ctx.voice_client and (
        ctx.voice_client.is_playing() or ctx.voice_client.is_paused()
    ):
        ctx.voice_client.stop()
        await ctx.send("تم التخطي، يتم تشغيل المقطع التالي.")
    else:
        await ctx.send("لا يوجد مقطع يمكن تخطيه.")


@bot.command(name="stop", help="إيقاف التشغيل ومغادرة الروم")
async def stop(ctx: commands.Context) -> None:
    if ctx.voice_client:
        if ctx.guild:
            await disconnect_player(ctx.guild.id, ctx.voice_client)
        await ctx.send("تم إيقاف التشغيل ومغادرة الروم.")
    else:
        await ctx.send("البوت غير موجود في روم صوتي.")


@bot.command(name="leave", help="مغادرة الروم الصوتي")
async def leave(ctx: commands.Context) -> None:
    await stop(ctx)


@bot.command(name="queue", aliases=["q"], help="عرض قائمة الانتظار")
async def queue(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    if not player.queue:
        await ctx.send("قائمة الانتظار فارغة.")
        return
    lines = [
        f"{index}. **{track.title}**"
        for index, track in enumerate(player.queue[:15], start=1)
    ]
    await ctx.send("\n".join(lines))


@bot.command(name="shuffle", help="خلط قائمة الانتظار")
async def shuffle(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    random.shuffle(player.queue)
    await ctx.send("تم خلط قائمة الانتظار.")


@bot.command(name="loop", help="تفعيل أو إيقاف تكرار المقطع")
async def loop(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    player.repeat = not player.repeat
    status = "مفعّل" if player.repeat else "متوقف"
    await ctx.send(f"التكرار الآن: **{status}**.")


@bot.command(name="volume", help="تغيير مستوى الصوت من 1 إلى 100")
async def volume(ctx: commands.Context, value: int) -> None:
    if not ctx.guild:
        return
    if not 1 <= value <= 100:
        await ctx.send("استخدم رقمًا من 1 إلى 100.")
        return
    player = get_player(ctx.guild.id)
    player.volume = value / 100
    if player.source:
        player.source.volume = player.volume
    await ctx.send(f"تم ضبط الصوت على {value}%.")


@bot.command(name="clear", help="مسح قائمة الانتظار")
async def clear(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    removed = len(player.queue)
    player.queue.clear()
    await ctx.send(f"تم مسح {removed} مقطع من قائمة الانتظار.")


@bot.command(name="now", help="عرض المقطع الحالي")
async def now(ctx: commands.Context) -> None:
    current = (
        players.get(ctx.guild.id).current.title
        if ctx.guild and ctx.guild.id in players and players[ctx.guild.id].current
        else None
    )
    if current:
        await ctx.send(f"المقطع الحالي: **{current}**")
    else:
        await ctx.send("لا يوجد مقطع يعمل حاليًا.")


@bot.command(name="panel", help="إظهار لوحة تحكم Spinosaurus")
async def panel(ctx: commands.Context) -> None:
    if not ctx.guild:
        await ctx.send("لوحة التحكم تعمل داخل السيرفر فقط.")
        return
    if not os.path.exists(PANEL_ART_PATH):
        await ctx.send(
            embed=panel_embed(ctx.guild.id),
            view=MusicPanel(ctx.guild.id),
        )
        return
    artwork = discord.File(PANEL_ART_PATH, filename=PANEL_ART_FILENAME)
    await ctx.send(
        embed=panel_embed(ctx.guild.id),
        view=MusicPanel(ctx.guild.id),
        file=artwork,
    )


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"استخدم `!{ctx.command.name} <القيمة المطلوبة>`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("القيمة غير صحيحة. جرّب الأمر مرة أخرى.")
        return
    if isinstance(error, commands.CommandInvokeError):
        logger.exception("Command %s failed", ctx.command, exc_info=error.original)
        await ctx.send("حصل خطأ أثناء تنفيذ الأمر.")
        return
    logger.exception("Unhandled command error", exc_info=error)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add the Discord bot token as a Replit Secret."
        )
    load_opus_library()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
