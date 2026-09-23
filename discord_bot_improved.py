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

PANEL_ART_PATH = "attached_assets/generated_images/spinosaurus_panel_banner.png"
PANEL_ART_FILENAME = "spinosaurus_panel_banner.png"
VOICE_STATE_PATH = "voice_state.json"

MAX_QUEUE_SIZE = 50
MAX_USER_QUEUE_SIZE = 10
MAX_TRACK_DURATION = 30 * 60
EXTRACT_TIMEOUT = 35
RECONNECT_DELAY = 5
MAX_RECONNECT_ATTEMPTS = 5

BASE_YTDL_OPTIONS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "socket_timeout": 15,
    "retries": 3,
    "fragment_retries": 3,
    "extractor_retries": 2,
    "file_access_retries": 2,
    "skip_unavailable_fragments": True,
    "ignoreerrors": False,
    "geo_bypass": True,
    "nocheckcertificate": False,
}

YOUTUBE_CLIENTS = [
    "web_embedded",
    "tv_simply",
    "android_vr",
    "tv",
    "ios",
    "android",
]

YTDL_OPTION_VARIANTS = []
for client in YOUTUBE_CLIENTS:
    options = dict(BASE_YTDL_OPTIONS)
    options["extractor_args"] = {
        "youtube": {
            "player_client": [client],
        }
    }
    YTDL_OPTION_VARIANTS.append(options)

cookies_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
po_token = os.getenv("YOUTUBE_PO_TOKEN", "").strip()

if cookies_file and os.path.exists(cookies_file):
    for options in YTDL_OPTION_VARIANTS:
        options["cookiefile"] = cookies_file

if po_token:
    for options in YTDL_OPTION_VARIANTS:
        youtube_args = dict(options.get("extractor_args", {}).get("youtube", {}))
        youtube_args["po_token"] = [f"web.gvs+{po_token}"]
        options["extractor_args"] = {"youtube": youtube_args}

_ytdl_instances = [yt_dlp.YoutubeDL(options) for options in YTDL_OPTION_VARIANTS]

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_at_eof 1 "
        "-reconnect_delay_max 5 "
        "-nostdin"
    ),
    "options": "-vn -sn -dn",
}


@dataclass
class Track:
    query: str
    title: str = "مقطع جديد"
    requested_by: str = ""
    requester_id: int = 0
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    webpage_url: Optional[str] = None


@dataclass
class GuildPlayer:
    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    source: Optional[discord.PCMVolumeTransformer] = None
    volume: float = 0.5
    repeat: bool = False
    voice_channel_id: Optional[int] = None
    keep_in_voice: bool = False
    voice_reconnect_task: Optional[asyncio.Task] = None
    reconnect_attempts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0


players: dict[int, GuildPlayer] = {}
voice_state_restored = False
registered_panel_guilds: set[int] = set()


def load_opus_library() -> None:
    if discord.opus.is_loaded():
        return

    candidates = [
        os.getenv("OPUS_LIBRARY", "").strip(),
        ctypes.util.find_library("opus") or "",
    ]

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run(
                ["ldd", ffmpeg],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            candidates.extend(
                re.findall(r"libopus\.so[^ ]* => (\S+)", result.stdout)
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

    raise RuntimeError("Opus library could not be loaded.")


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def load_voice_state() -> None:
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
            logger.warning(
                "Ignoring invalid saved voice state: %r -> %r",
                guild_id,
                channel_id,
            )


def save_voice_state() -> None:
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


def format_duration(seconds: int) -> str:
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


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

    embed.add_field(
        name="Spinosaurus Music",
        value="\n".join(details),
        inline=False,
    )
    embed.set_footer(text="PREHISTORIC RADIO • استخدم !music لإضافة أغنية")
    return embed


def normalize_query(query: str) -> str:
    query = query.strip()
    query = re.sub(r"\s+", " ", query)
    return query[:1000]


def is_youtube_url(query: str) -> bool:
    return bool(
        re.match(
            r"^https?://(?:(?:www|music|m)\.)?(?:youtube\.com|youtu\.be)/",
            query,
            re.IGNORECASE,
        )
    )


def is_allowed_query(query: str) -> bool:
    if not query:
        return False
    if is_youtube_url(query):
        return True
    return len(query) <= 200


def extract_entries(data):
    if not data:
        return None

    entries = data.get("entries")
    if entries is not None:
        entries = [entry for entry in entries if entry]
        return entries[0] if entries else None

    return data


async def extract_audio(
    search: str,
) -> tuple[discord.AudioSource, str, Optional[str], Optional[int], Optional[str]]:
    loop = asyncio.get_running_loop()
    search = normalize_query(search)
    last_error: Optional[Exception] = None

    for index, extractor in enumerate(_ytdl_instances):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda extractor=extractor: extractor.extract_info(
                        search,
                        download=False,
                    ),
                ),
                timeout=EXTRACT_TIMEOUT,
            )

            data = extract_entries(data)

            if not data:
                raise RuntimeError("لم يتم العثور على المقطع.")

            stream_url = data.get("url")
            title = data.get("title") or "مقطع صوتي"
            duration = data.get("duration")
            webpage_url = data.get("webpage_url") or data.get("original_url")

            if not stream_url:
                raise RuntimeError("تعذر الحصول على رابط الصوت.")

            if duration is not None:
                try:
                    duration = int(duration)
                except (TypeError, ValueError):
                    duration = None

            if duration and duration > MAX_TRACK_DURATION:
                raise RuntimeError(
                    f"مدة المقطع تتجاوز الحد المسموح ({MAX_TRACK_DURATION // 60} دقيقة)."
                )

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    stream_url,
                    **FFMPEG_OPTIONS,
                ),
                volume=0.5,
            )

            logger.info(
                "Extracted audio using configuration #%s for %r",
                index + 1,
                search,
            )

            return (
                source,
                title,
                data.get("thumbnail"),
                duration,
                webpage_url,
            )

        except asyncio.TimeoutError as exc:
            last_error = exc
            logger.warning(
                "yt-dlp configuration #%s timed out for %r",
                index + 1,
                search,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "yt-dlp configuration #%s failed for %r: %s",
                index + 1,
                search,
                exc,
            )

    if last_error:
        raise RuntimeError("تعذر استخراج الصوت من YouTube.") from last_error

    raise RuntimeError("تعذر استخراج الصوت من YouTube.")


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
    player.reconnect_attempts = 0
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
    player = get_player(guild_id)

    while player.keep_in_voice:
        await asyncio.sleep(RECONNECT_DELAY)

        if not player.keep_in_voice or not player.voice_channel_id:
            return

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        channel = guild.get_channel(player.voice_channel_id)

        if not isinstance(channel, discord.VoiceChannel):
            logger.warning(
                "Voice channel %s is no longer available",
                player.voice_channel_id,
            )
            return

        voice_client = guild.voice_client

        if voice_client and voice_client.is_connected():
            player.reconnect_attempts = 0
            return

        if player.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "Maximum voice reconnect attempts reached for guild %s",
                guild_id,
            )
            player.reconnect_attempts = 0
            return

        player.reconnect_attempts += 1

        try:
            if voice_client:
                await voice_client.disconnect(force=True)

            voice_client = await channel.connect(reconnect=True)

            if player.current:
                interrupted = player.current
                player.queue.insert(0, interrupted)
                player.current = None
                player.source = None

            await start_next(guild_id, voice_client)
            player.reconnect_attempts = 0
            logger.info("Reconnected to voice channel %s", channel.id)
            return

        except (discord.ClientException, asyncio.TimeoutError):
            logger.exception(
                "Could not reconnect to voice channel %s",
                channel.id,
            )


async def disconnect_player(
    guild_id: int,
    voice_client: Optional[discord.VoiceClient],
) -> None:
    player = get_player(guild_id)
    player.keep_in_voice = False
    player.voice_channel_id = None
    player.reconnect_attempts = 0
    player.generation += 1
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
        try:
            await voice_client.disconnect(force=True)
        except (discord.ClientException, asyncio.TimeoutError):
            logger.exception("Could not disconnect voice client")


async def start_next(
    guild_id: int,
    voice_client: discord.VoiceClient,
    announce_channel: Optional[discord.abc.Messageable] = None,
) -> bool:
    player = get_player(guild_id)

    async with player.lock:
        if voice_client.is_playing() or voice_client.is_paused():
            return False

        while True:
            if player.current and player.repeat:
                track = player.current
            elif player.queue:
                track = player.queue.pop(0)
                player.current = track
            else:
                player.current = None
                player.source = None
                return False

            try:
                if announce_channel:
                    await announce_channel.send(
                        f"⏳ جاري تجهيز: **{track.title}**",
                        delete_after=15,
                    )

                (
                    source,
                    title,
                    thumbnail,
                    duration,
                    webpage_url,
                ) = await extract_audio(track.query)

            except Exception as exc:
                logger.exception(
                    "Could not extract audio for %r: %s",
                    track.query,
                    exc,
                )

                player.current = None
                player.source = None

                if announce_channel:
                    await announce_channel.send(
                        f"❌ تعذر تشغيل **{track.title}**. "
                        "قد يكون المقطع غير متاح أو رفض YouTube الوصول إليه.",
                        delete_after=20,
                    )

                if not player.queue:
                    return False

                continue

            track.title = title
            track.thumbnail = thumbnail
            track.duration = duration
            track.webpage_url = webpage_url

            source.volume = player.volume
            player.source = source
            generation = player.generation

            def after_playback(
                error: Optional[Exception],
                guild_id: int = guild_id,
                voice_client: discord.VoiceClient = voice_client,
                announce_channel: Optional[discord.abc.Messageable] = announce_channel,
                generation: int = generation,
            ) -> None:
                if error:
                    logger.error("Playback error: %s", error)

                if generation != get_player(guild_id).generation:
                    return

                future = asyncio.run_coroutine_threadsafe(
                    start_next(
                        guild_id,
                        voice_client,
                        announce_channel,
                    ),
                    bot.loop,
                )

                def done_callback(completed: asyncio.Future) -> None:
                    try:
                        error_result = completed.exception()
                    except asyncio.CancelledError:
                        return

                    if error_result:
                        logger.error(
                            "Could not start queued track: %s",
                            error_result,
                        )

                future.add_done_callback(done_callback)

            try:
                voice_client.play(source, after=after_playback)
            except Exception:
                player.source = None
                logger.exception("Voice client could not start playback")
                if not player.queue:
                    return False
                continue

            if announce_channel:
                await announce_channel.send(
                    f"🎵 شغال الآن: **{title}**",
                    delete_after=20,
                )

            return True


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
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                item.custom_id = f"{item.custom_id}:{guild_id}"

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
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
        custom_id="music_panel_play",
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
        custom_id="music_panel_pause",
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
        custom_id="music_panel_skip",
    )
    async def skip(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        voice_client = interaction.guild.voice_client

        if voice_client and (
            voice_client.is_playing() or voice_client.is_paused()
        ):
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
        custom_id="music_panel_stop",
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
        custom_id="music_panel_queue",
    )
    async def show_queue(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        player = get_player(self.guild_id)

        items = [
            f"{index}. {track.title}"
            for index, track in enumerate(
                player.queue[:10],
                start=1,
            )
        ]

        queue_text = "\n".join(items) if items else "القائمة فارغة."

        await interaction.response.send_message(
            queue_text,
            ephemeral=True,
        )

    @discord.ui.button(
        label="تكرار",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="music_panel_repeat",
    )
    async def toggle_repeat(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        player = get_player(self.guild_id)
        player.repeat = not player.repeat
        button.label = (
            "التكرار: تشغيل"
            if player.repeat
            else "التكرار: إيقاف"
        )

        await interaction.response.edit_message(
            embed=panel_embed(self.guild_id),
            view=self,
        )

    @discord.ui.button(
        label="خلط",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="music_panel_shuffle",
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
        custom_id="music_panel_volume",
    )
    async def volume(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            VolumeModal(self.guild_id)
        )

    @discord.ui.button(
        label="مساعدة",
        emoji="❔",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="music_panel_help",
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
            "`!clear` مسح القائمة\n"
            "`!stop` إيقاف ومغادرة",
            ephemeral=True,
        )


@bot.event
async def on_ready() -> None:
    global voice_state_restored

    if not bot.user:
        return

    desired_name = os.getenv("BOT_NAME", "Spinosaurus").strip()

    if desired_name and bot.user.name != desired_name:
        try:
            await bot.user.edit(username=desired_name)
        except discord.HTTPException:
            logger.exception("Could not change bot username")

    await bot.change_presence(
        activity=discord.Game(name="🦖 !panel • Prehistoric Radio")
    )

    if not voice_state_restored:
        load_voice_state()
        voice_state_restored = True

    for guild in bot.guilds:
        if guild.id not in registered_panel_guilds:
            bot.add_view(MusicPanel(guild.id))
            registered_panel_guilds.add(guild.id)

    for guild_id, player in players.items():
        if player.keep_in_voice and player.voice_channel_id:
            schedule_voice_reconnect(guild_id)

    logger.info(
        "Bot is online as %s (id=%s)",
        bot.user,
        bot.user.id,
    )


@bot.event
async def on_disconnect() -> None:
    logger.warning(
        "Discord gateway disconnected; discord.py will reconnect automatically"
    )


@bot.event
async def on_resumed() -> None:
    logger.info("Discord gateway session resumed")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not bot.user or member.id != bot.user.id:
        return

    if before.channel is None:
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
@commands.cooldown(1, 3, commands.BucketType.user)
async def play(ctx: commands.Context, *, search: str) -> None:
    search = normalize_query(search)

    if not is_allowed_query(search):
        await ctx.send("اكتب اسم أغنية أو رابط YouTube صحيح.")
        return

    voice_client = await ensure_voice(ctx)

    if voice_client is None or not ctx.guild:
        return

    player = get_player(ctx.guild.id)

    if len(player.queue) >= MAX_QUEUE_SIZE:
        await ctx.send(
            f"القائمة وصلت للحد الأقصى: {MAX_QUEUE_SIZE} مقطع."
        )
        return

    user_queued = sum(
        1
        for track in player.queue
        if track.requester_id == ctx.author.id
    )

    if user_queued >= MAX_USER_QUEUE_SIZE:
        await ctx.send(
            f"يمكنك وضع {MAX_USER_QUEUE_SIZE} مقاطع كحد أقصى في القائمة."
        )
        return

    track = Track(
        query=search,
        title=search[:100],
        requested_by=getattr(ctx.author, "display_name", ""),
        requester_id=ctx.author.id,
    )

    was_idle = (
        not voice_client.is_playing()
        and not voice_client.is_paused()
        and player.current is None
    )

    player.queue.append(track)

    if was_idle:
        await start_next(
            ctx.guild.id,
            voice_client,
            ctx.channel,
        )
    else:
        await ctx.send("تمت إضافة المقطع إلى قائمة الانتظار.")


@play.error
async def play_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"انتظر {error.retry_after:.1f} ثانية قبل طلب أغنية أخرى."
        )
        return
    raise error


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
        ctx.voice_client.is_playing()
        or ctx.voice_client.is_paused()
    ):
        ctx.voice_client.stop()
        await ctx.send("تم التخطي، يتم تشغيل المقطع التالي.")
    else:
        await ctx.send("لا يوجد مقطع يمكن تخطيه.")


@bot.command(name="stop", help="إيقاف التشغيل ومغادرة الروم")
async def stop(ctx: commands.Context) -> None:
    if ctx.voice_client:
        if ctx.guild:
            await disconnect_player(
                ctx.guild.id,
                ctx.voice_client,
            )
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
        for index, track in enumerate(
            player.queue[:15],
            start=1,
        )
    ]

    await ctx.send("\n".join(lines))


@bot.command(name="shuffle", help="خلط قائمة الانتظار")
async def shuffle(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = get_player(ctx.guild.id)

    if not player.queue:
        await ctx.send("القائمة فارغة.")
        return

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
    if not ctx.guild:
        return

    player = get_player(ctx.guild.id)
    current = player.current.title if player.current else None

    if current:
        await ctx.send(f"المقطع الحالي: **{current}**")
    else:
        await ctx.send("لا يوجد مقطع يعمل حاليًا.")


@bot.command(name="panel", help="إظهار لوحة تحكم Spinosaurus")
async def panel(ctx: commands.Context) -> None:
    if not ctx.guild:
        await ctx.send("لوحة التحكم تعمل داخل السيرفر فقط.")
        return

    view = MusicPanel(ctx.guild.id)

    if not os.path.exists(PANEL_ART_PATH):
        await ctx.send(
            embed=panel_embed(ctx.guild.id),
            view=view,
        )
        return

    artwork = discord.File(
        PANEL_ART_PATH,
        filename=PANEL_ART_FILENAME,
    )

    await ctx.send(
        embed=panel_embed(ctx.guild.id),
        view=view,
        file=artwork,
    )


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"انتظر {error.retry_after:.1f} ثانية."
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"استخدم `!{ctx.command.name} <القيمة المطلوبة>`."
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            "القيمة غير صحيحة. جرّب الأمر مرة أخرى."
        )
        return

    if isinstance(error, commands.CommandInvokeError):
        logger.exception(
            "Command %s failed",
            ctx.command,
            exc_info=error.original,
        )
        await ctx.send(
            "حصل خطأ أثناء تنفيذ الأمر."
        )
        return

    logger.exception(
        "Unhandled command error",
        exc_info=error,
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    load_opus_library()
    bot.run(
        token,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
