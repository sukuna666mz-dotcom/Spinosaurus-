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

queues = {}         # guild_id -> list of queries
volumes = {}        # guild_id -> float (0.1 - 1.5)
now_playing = {}    # guild_id -> title
panels = {}         # guild_id -> panel message
current_query = {}  # guild_id -> query string of the track currently/last playing
loop_mode = {}      # guild_id -> bool, True = repeat current track forever


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


async def delete_panel(guild_id):
    old = panels.pop(guild_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass


async def stop_all(guild):
    queues[guild.id] = []
    now_playing.pop(guild.id, None)
    current_query.pop(guild.id, None)
    loop_mode[guild.id] = False
    await delete_panel(guild.id)
    if guild.voice_client:
        await guild.voice_client.disconnect()


def build_embed(guild_id):
    title = now_playing.get(guild_id, "—")
    vol = int(volumes.get(guild_id, 0.5) * 100)
    q = queues.get(guild_id, [])
    loop_on = loop_mode.get(guild_id, False)
    embed = discord.Embed(title="🎶 الآن يعمل", description=f"**{title}**", color=0xFF5500)
    embed.add_field(name="🔊 الصوت", value=f"{vol}%", inline=True)
    embed.add_field(name="📜 في الطابور", value=str(len(q)), inline=True)
    embed.add_field(name="🔂 التكرار", value="مفعّل ✅" if loop_on else "متوقف", inline=True)
    embed.set_footer(text="SoundCloud")
    return embed


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

    @discord.ui.button(emoji="⏯️", label="إيقاف/تشغيل", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            msg = "▶️ تم الاستئناف"
        elif vc.is_playing():
            vc.pause()
            msg = "⏸️ إيقاف مؤقت"
        else:
            msg = "ما فيه شيء يشتغل."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="تخطي", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            loop_mode[gid] = False  # skipping cancels the loop on the current track
            vc.stop()
            await interaction.response.send_message("⏭️ تم التخطي", ephemeral=True)
        else:
            await interaction.response.send_message("ما فيه شيء يشتغل.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="إيقاف", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏹️ تم الإيقاف", ephemeral=True)
        await stop_all(interaction.guild)

    @discord.ui.button(emoji="🔉", label="أخفض", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, -0.1)

    @discord.ui.button(emoji="🔊", label="أعلى", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, +0.1)

    @discord.ui.button(emoji="🔂", label="تكرار", style=discord.ButtonStyle.secondary, row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        new_state = not loop_mode.get(gid, False)
        loop_mode[gid] = new_state
        button.style = discord.ButtonStyle.success if new_state else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=build_embed(gid), view=self)

    @discord.ui.button(emoji="📜", label="الطابور", style=discord.ButtonStyle.secondary, row=1)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid, [])
        if not q:
            return await interaction.response.send_message("الطابور فاضي.", ephemeral=True)
        lines = [f"{i}. {item[:60]}" for i, item in enumerate(q[:10], 1)]
        if len(q) > 10:
            lines.append(f"... و{len(q) - 10} أخرى")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def change_volume(self, interaction: discord.Interaction, delta):
        gid = interaction.guild.id
        vol = round(min(1.5, max(0.1, volumes.get(gid, 0.5) + delta)), 2)
        volumes[gid] = vol
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = vol
        await interaction.response.edit_message(embed=build_embed(gid), view=self)


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

    query = q.pop(0)
    try:
        url, title = await bot.loop.run_in_executor(None, fetch_stream, query)
    except Exception as e:
        await channel.send(f"❌ فشل: {e}")
        return await play_next(guild, channel)

    def after(err):
        if loop_mode.get(gid):
            queues.setdefault(gid, []).insert(0, query)
        asyncio.run_coroutine_threadsafe(play_next(guild, channel), bot.loop)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(url, **FFMPEG_OPTS), volume=volumes.get(gid, 0.5)
    )
    vc.play(source, after=after)
    now_playing[gid] = title
    current_query[gid] = query

    await delete_panel(gid)
    panels[gid] = await channel.send(embed=build_embed(gid), view=ControlPanel(gid))


@bot.command(name="music", aliases=["play", "p"])
async def music(ctx, *, query):
    if not ctx.author.voice:
        return await ctx.send("ادخل روم صوتي أول.")
    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()
    queues.setdefault(ctx.guild.id, []).append(query)
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


@bot.command()
async def resume(ctx):
    if ctx.voice_client:
        ctx.voice_client.resume()


@bot.command()
async def stop(ctx):
    if ctx.voice_client:
        await stop_all(ctx.guild)
        await ctx.send("⏹️ تم الإيقاف")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


load_opus()
bot.run(TOKEN)
