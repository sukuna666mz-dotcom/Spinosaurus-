import os
import discord
from discord.ext import commands

# إعدادات تشغيل الصوت لاستقرار البث
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"البوت يعمل بنجاح: {bot.user}")

@bot.command(name="play")
async def play(ctx: commands.Context, *, url: str):
    if not ctx.author.voice:
        return await ctx.send("يجب أن تكون في روم صوتي أولاً!")

    voice_channel = ctx.author.voice.channel
    
    if not ctx.voice_client:
        await voice_channel.connect()
    
    vc = ctx.voice_client

    if vc.is_playing():
        vc.stop()

    try:
        source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        vc.play(source, after=lambda e: print(f"خطأ في التشغيل: {e}" if e else None))
        await ctx.send("🎶 جاري تشغيل الرابط المطلوب بنجاح!")
    except Exception as e:
        await ctx.send(f"حدث خطأ أثناء التشغيل: {e}")

@bot.command(name="leave")
async def leave(ctx: commands.Context):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("تم الخروج من الروم الصوتي.")

def main() -> None:
    # قراءة التوكن من إعدادات البيئة في الاستضافة (Railway Variables)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Please add it to your environment variables.")
    
    bot.run(token)

if __name__ == "__main__":
    main()
    
