import discord
from discord.ext import commands
import requests

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command(name="play")
async def play(ctx: commands.Context, *, url: str):
    if not ctx.author.voice:
        return await ctx.send("You must be in a voice channel first!")

    voice_channel = ctx.author.voice.channel
    if not ctx.voice_client:
        await voice_channel.connect()
    
    vc = ctx.voice_client
    if vc.is_playing():
        vc.stop()

    try:
        # فك الرابط القصير (مثل روابط on.soundcloud.com) لجلب الرابط الحقيقي تلقائياً
        if "on.soundcloud.com" in url:
            response = requests.get(url, allow_redirects=True)
            url = response.url

        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        vc.play(source, after=lambda e: print(f"Error: {e}" if e else None))
        await ctx.send("🎶 جاري تشغيل الرابط بنجاح!")
    except Exception as e:
        await ctx.send(f"حدث خطأ أثناء التشغيل: {e}")

@bot.command(name="leave")
async def leave(ctx: commands.Context):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Disconnected from voice channel.")

bot.run("MTU1MDg1NzYxMzI2NzMwNDQ4OQ.Gx3Tnt.V0Cf4tCEdj5rXKDjuAcEiHMaIUZ7iioD74JMJw")
        
