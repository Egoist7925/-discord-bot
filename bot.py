import discord
from discord.ext import commands
import json
import os
import time

TOKEN = os.getenv("TOKEN")

TARGET_USER_ID = 1550269164843827362

MUSIC_FILE = "ride.wav"
POSITION_FILE = "position.json"

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def load_position():
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_position(position):
    with open(POSITION_FILE, "w", encoding="utf-8") as f:
        json.dump(position, f)


positions = load_position()

# 現在の再生開始位置と時刻
play_start_position = {}
play_start_time = {}


@bot.event
async def on_ready():
    print(f"{bot.user} でログインしました")


@bot.event
async def on_voice_state_update(member, before, after):

    if member.id != TARGET_USER_ID:
        return

    # ユーザーがVCに入った
    if before.channel is None and after.channel is not None:

        channel = after.channel
        guild_id = str(channel.guild.id)

        if channel.guild.voice_client is not None:
            return

        print(f"{member.display_name} がVCに入りました")

        voice_client = await channel.connect()

        # 前回の位置
        start_time = positions.get(guild_id, 0)

        print(f"{start_time:.2f}秒から再生します")

        source = discord.FFmpegPCMAudio(
            MUSIC_FILE,
            before_options=f"-ss {start_time}"
        )

        voice_client.play(source)

        # 再生開始位置と時刻を記録
        play_start_position[guild_id] = start_time
        play_start_time[guild_id] = time.monotonic()

        print("ride.wavを再生しました")

    # ユーザーがVCから退出
    elif before.channel is not None and after.channel is None:

        voice_client = before.channel.guild.voice_client

        if voice_client is not None:

            guild_id = str(before.channel.guild.id)

            # 再生開始位置 + 実際に経過した時間
            if guild_id in play_start_time:

                elapsed = time.monotonic() - play_start_time[guild_id]
                current_position = play_start_position[guild_id] + elapsed

                positions[guild_id] = current_position
                save_position(positions)

                print(f"{current_position:.2f}秒で保存しました")

            await voice_client.disconnect()

            print("指定ユーザーが退出したのでBotも退出しました")


bot.run(TOKEN)