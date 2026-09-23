import os
import discord
from discord.ext import commands

# إعدادات تشغيل الصوت لاستقرار البث وعدم الانقطاع
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# تهيئة البوت مع صلاحيات كاملة
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"البوت يعمل بنجاح: {bot.user}")

@bot.command(name="play")
async def play(ctx: commands.Context, *, url: str):
    # التحقق من وجود المستخدم في روم صوتي
    if not ctx.author.voice:
        return await ctx.send("يجب أن تكون في روم صوتي أولاً!")

    voice_channel = ctx.author.voice.channel
    
    # الانضمام للروم الصوتي إذا لم يكن متصلاً
    if not ctx.voice_client:
        await voice_channel.connect()
    
    vc = ctx.voice_client

    # إيقاف أي صوت قديم يعمل حالياً
    if vc.is_playing():
        vc.stop()

    try:
        # تشغيل الصوت مباشرة بدون مشاكل يوتيوب القديمة
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
    # استخدام التوكن الخاص بك مباشرة
    token = "MTU1MDg1NzYxMzI2NzMwNDQ4OQ.Gx3Tnt.V0Cf4tCEdj5rXKDjuAcEiHMaIUZ7iioD74JMJw"
    if not token:
        raise RuntimeError("BOT_TOKEN is missing.")
    
    bot.run(token)

if __name__ == "__main__":
    main()
    
