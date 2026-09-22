
import os
import random
import re
import time
import unicodedata

import discord
from discord.ext import commands
import yt_dlp


# =========================================================
# 基本設定
# =========================================================
# Bot Tokenはコードへ直接書かず、環境変数から読み込む
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
TARGET_USER_NAME = "819ego"

COOKIE_FILE = "cookies.txt"


def add_cookie(opts):
    """cookies.txt が存在するときだけ yt-dlp にCookieを渡す。"""
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts

PLAYLIST_URLS = {
    1: "https://youtube.com/playlist?list=PLFFoeaVOrPUc&si=c0tz_TQvIRcShAOd",
    2: "https://youtube.com/playlist?list=PL4j925OzRK0YEJQWR0GeaZeP8IZ9YvwFx&si=s8Kxfq694_gvqijF",
    3: "ここに将来用3つ目のプレイリストURLを入れる",
}

# 関連曲候補に含まれていた場合、60%の確率で優先するワード
HIGH_PRIORITY_KEYWORDS = [
    "watson",
    "ワトソン",
    "yvng patra",
    "ヤングパトラ",
]

# 特定アーティスト優先率
HIGH_PRIORITY_RATE = 0.60

# 関連曲検索で、一度にプレイリスト全体から何曲を参照するか
# 毎回この数だけ全曲からランダムに選ぶので、同じ系統に固定されにくい
RELATED_LIBRARY_SAMPLE_SIZE = 10

# 1回の関連曲選曲で実際にYouTube検索へ投げる検索数
RELATED_QUERY_COUNT = 6

# 1つの検索語につき取得する候補数
RELATED_SEARCH_RESULTS = 10


# =========================================================
# Discord Bot
# =========================================================
intents = discord.Intents.all()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents)


# =========================================================
# 再生状態
# =========================================================
selected_playlist = 1
playlist_cache = []
current_track_index = 0

# 選択中プレイリスト全体を、関連曲選びの参照ライブラリーとして保持
related_library = []
related_mode = False
related_history = set()
related_recent_channels = []
related_recent_song_keys = []

skip_requested = set()
play_transition_lock = asyncio.Lock()
play_history = []
auto_join = True
last_played_track = None
resume_position_sec = 0.0
play_started_monotonic = None


# =========================================================
# プレイリスト取得
# =========================================================
def fetch_playlist(url):
    if not url or not url.startswith("http"):
        return []

    opts = add_cookie({
        "extract_flat": "in_playlist",
        "quiet": True,
        "skip_download": True,
        "js_runtimes": {"deno": {}},
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        tracks = []

        for entry in info.get("entries", []):
            if entry and entry.get("id"):
                tracks.append(
                    {
                        "url": f"https://www.youtube.com/watch?v={entry['id']}",
                        "title": entry.get("title", "Unknown"),
                    }
                )

        return tracks

    except Exception as e:
        print(f"Playlist Error: {e}")
        return []


async def get_playlist():
    loop = asyncio.get_running_loop()
    target_url = PLAYLIST_URLS.get(selected_playlist)

    return await loop.run_in_executor(
        None,
        fetch_playlist,
        target_url if target_url else "",
    )


# =========================================================
# 音源URL取得
# =========================================================
def fetch_stream(url):
    opts = add_cookie({
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {}},
        "nocheckcertificate": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("url"), info.get("title", "Unknown")

    except Exception as e:
        print(f"Stream Error: {e}")
        return None, None


async def get_stream(url):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_stream, url)


# =========================================================
# 関連曲検索
# =========================================================
def _get_channel_key(entry):
    """検索結果から投稿者を識別するためのキーを取る。"""
    return (
        entry.get("channel_id")
        or entry.get("uploader_id")
        or entry.get("channel")
        or entry.get("uploader")
        or ""
    )


def _is_priority_track(entry):
    text = " ".join(
        [
            entry.get("title", ""),
            entry.get("channel", ""),
            entry.get("uploader", ""),
        ]
    ).lower()

    return any(keyword.lower() in text for keyword in HIGH_PRIORITY_KEYWORDS)


async def search_related_from_library():
    global related_recent_song_keys
    """
    プレイリスト終了後の関連曲選択。

    35%: Yvng Patra をYouTubeで直接検索して選曲
    35%: Watson をYouTubeで直接検索して選曲
    30%: 選択中プレイリスト全体を参照した関連曲選曲

    元プレイリスト内の曲・関連曲履歴・LIVE系はできるだけ避ける。
    """
    global related_recent_channels

    if not related_library:
        print("関連曲ライブラリーが空です。")
        return None

    opts = add_cookie({
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {}},
    })

    playlist_ids = {
        track.get("url", "").split("v=")[-1].split("&")[0]
        for track in related_library
        if track.get("url")
    }

    # ---------------------------------------------------------
    # 音楽以外を関連候補から除外
    # ---------------------------------------------------------
    # ytsearch の検索結果には、同じアーティスト名を含む
    # インタビュー・リアクション・番組・切り抜き等も混ざるため、
    # タイトル/投稿者名と動画時間で弾く。
    NON_MUSIC_WORDS = (
        "海外の反応",
        "reaction",
        "reacts",
        "reacting",
        "リアクション",
        "interview",
        "インタビュー",
        "podcast",
        "ポッドキャスト",
        "magazine",
        "documentary",
        "ドキュメンタリー",
        "behind the scenes",
        "making of",
        "メイキング",
        "密着",
        "対談",
        "座談会",
        "トーク",
        "talk show",
        "radio",
        "ラジオ",
        "解説",
        "考察",
        "レビュー",
        "review",
        "ニュース",
        "news",
        "切り抜き",
        "clip channel",
        "full episode",
        "episode full",
        "ゲスト:",
        "ゲスト：",
        "shorts",
        "#shorts",
    )

    # 関連曲としては長すぎる動画を除外。
    # 45秒未満の極端に短い動画も Shorts / 告知の可能性が高いので除外。
    MIN_MUSIC_DURATION = 45
    MAX_MUSIC_DURATION = 15 * 60

    def _looks_like_music(entry):
        title = entry.get("title", "") or ""
        haystack = " ".join([
            title,
            entry.get("channel", "") or "",
            entry.get("uploader", "") or "",
        ]).lower()

        if any(word in haystack for word in NON_MUSIC_WORDS):
            return False

        # 以前からLIVE系は関連候補から避ける仕様を維持。
        if "live" in title.lower():
            return False

        duration = entry.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            if duration < MIN_MUSIC_DURATION or duration > MAX_MUSIC_DURATION:
                return False

        return True

    def _canonical_song_key(entry):
        """
        同じ曲の別動画を同一扱いするためのキーを作る。
        例:
          Watson - MJ Freestyle (Official Video)
          MJ Freestyle - Watson
        は、どちらもほぼ同じキーになる。
        """
        title = unicodedata.normalize(
            "NFKC", (entry.get("title") or "")
        ).lower()

        # MV / Audio / Visualizer 等の動画形式表記を除去
        noise_phrases = (
            "official music video",
            "official video",
            "official audio",
            "official mv",
            "music video",
            "performance video",
            "visualizer",
            "lyric video",
            "lyrics video",
            "lyrics",
            "lyric",
            "audio",
            "mv",
            "pv",
        )
        for phrase in noise_phrases:
            title = title.replace(phrase, " ")

        # 括弧内が動画形式・画質などの付加情報なら丸ごと除去しやすくする。
        # feat. など曲名として有用な部分は、括弧の外ならそのまま残る。
        title = re.sub(
            r"[\(\[【（][^\)\]】）]*(?:official|video|audio|mv|visualizer|lyrics?|4k|hd)[^\)\]】）]*[\)\]】）]",
            " ",
            title,
            flags=re.IGNORECASE,
        )

        # 直接検索している2アーティスト名は曲識別から外す。
        # これで「Watson - 曲名」と「曲名 - Watson」の順序差を吸収できる。
        for artist_name in (
            "yvng patra", "ヤングパトラ", "watson", "ワトソン"
        ):
            title = title.replace(artist_name, " ")

        # 記号をスペース化して、単語順も正規化。
        title = re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠々ー]+", " ", title)
        words = [w for w in title.split() if w]

        if not words:
            # ほぼ空になった時だけ元タイトルを軽く正規化して利用
            raw = unicodedata.normalize(
                "NFKC", (entry.get("title") or "")
            ).lower()
            raw = re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠々ー]+", " ", raw)
            words = [w for w in raw.split() if w]

        return " ".join(sorted(words))

    def _filter_candidates(results):
        unique_results = {}
        for entry in results or []:
            if entry and entry.get("id"):
                unique_results[entry["id"]] = entry

        candidates = [
            entry
            for entry in unique_results.values()
            if entry.get("id")
            and entry["id"] not in related_history
            and entry["id"] not in playlist_ids
            and _canonical_song_key(entry) not in related_recent_song_keys
            and _looks_like_music(entry)
        ]

        # 同じ投稿者が連続しすぎないようにする
        varied = [
            entry
            for entry in candidates
            if not _get_channel_key(entry)
            or _get_channel_key(entry) not in related_recent_channels
        ]
        return varied or candidates

    async def _direct_artist_search(artist):
        loop = asyncio.get_running_loop()

        def _search():
            with yt_dlp.YoutubeDL(opts) as ydl:
                # artist名を明示して検索。複数語で候補を広げる。
                if artist.lower() == "watson":
                    # 日本のラッパー Watson に検索を寄せる。
                    queries = [
                        'ytsearch20:"Watson" 徳島 rapper',
                        'ytsearch20:"Watson" 日本 ラッパー',
                        'ytsearch15:"Watson" MJ Freestyle',
                        'ytsearch15:"Watson" Official Video hiphop',
                    ]
                else:
                    queries = [
                        f'ytsearch15:"{artist}" official audio',
                        f'ytsearch15:"{artist}" music',
                        f'ytsearch10:"{artist}"',
                    ]
                found = []
                for query in queries:
                    try:
                        info = ydl.extract_info(query, download=False)
                        found.extend(info.get("entries") or [])
                    except Exception as e:
                        print(f"{artist} Search Warning: {e}")
                return found

        results = await loop.run_in_executor(None, _search)
        candidates = _filter_candidates(results)

        # タイトル・投稿者にアーティスト名が含まれるものを優先。
        # Watson は同名アーティストが多いため、明らかな別人を除外する。
        artist_lower = artist.lower()
        exactish = []

        watson_excludes = (
            "patrick watson",
            "amii watson",
            "doc watson",
            "johnny watson",
            "gene watson",
            "willie watson",
            "watson b2b",
            "watson-g",
            "watson g",
            "house mix",
            "haitian",
            "musiques compilation",
        )

        for entry in candidates:
            haystack = " ".join([
                entry.get("title", ""),
                entry.get("channel", ""),
                entry.get("uploader", ""),
            ]).lower()

            if artist_lower not in haystack:
                continue

            if artist_lower == "watson":
                if any(bad in haystack for bad in watson_excludes):
                    continue

                title_lower = entry.get("title", "").lower().strip()
                channel_lower = " ".join([
                    entry.get("channel", ""),
                    entry.get("uploader", ""),
                ]).lower()

                # 「Watson - 曲名」「曲名 - Watson」系、または
                # 日本/徳島/hiphop文脈を持つ候補だけを通す。
                watson_title_form = (
                    title_lower.startswith("watson -")
                    or title_lower.startswith("watson「")
                    or title_lower.startswith("watson 『")
                    or title_lower.endswith(" - watson")
                )
                watson_context = any(
                    key in haystack
                    for key in ("徳島", "japanese", "日本", "hiphop", "hip hop", "ラッパー")
                )

                if not watson_title_form and not watson_context:
                    continue

            exactish.append(entry)

        # 直接アーティスト枠では、本人らしい候補が無いときに
        # 無関係な検索結果を採用しない。
        candidates = exactish
        if not candidates:
            return None

        return random.choice(candidates)

    async def _normal_library_search():
        seed_tracks = random.sample(
            related_library,
            min(RELATED_LIBRARY_SAMPLE_SIZE, len(related_library)),
        )

        queries = []
        for track in seed_tracks:
            title = track.get("title", "").strip()
            if not title or title == "Unknown":
                continue
            queries.extend([
                f'"{title}" similar songs',
                f'"{title}" recommendations',
                f'"{title}" related music',
            ])

        valid_titles = [
            track.get("title", "").strip()
            for track in seed_tracks
            if track.get("title", "").strip()
            and track.get("title", "").strip() != "Unknown"
        ]

        if len(valid_titles) >= 2:
            combo = " ".join(f'"{title}"' for title in valid_titles[:3])
            queries.extend([
                f"{combo} similar artists",
                f"{combo} mix",
            ])

        queries = list(dict.fromkeys(queries))
        if not queries:
            return None

        selected_queries = random.sample(
            queries,
            min(RELATED_QUERY_COUNT, len(queries)),
        )

        loop = asyncio.get_running_loop()

        def _search():
            all_results = []
            with yt_dlp.YoutubeDL(opts) as ydl:
                for query in selected_queries:
                    try:
                        info = ydl.extract_info(
                            f"ytsearch{RELATED_SEARCH_RESULTS}:{query}",
                            download=False,
                        )
                        all_results.extend(info.get("entries") or [])
                    except Exception as e:
                        print(f"Related Search Warning: {e}")
            return all_results

        results = await loop.run_in_executor(None, _search)
        candidates = _filter_candidates(results)
        if not candidates:
            return None

        # その他30%枠は、公式MV / Official Audio / Topic系を最優先。
        # 音楽以外の候補は _filter_candidates() ですでに除外済み。
        # ここで全滅した場合も、フィルタ前候補を復活させない。
        avoid_words = (
            "cover", "歌ってみた", "字幕", "lyrics", "lyric",
            "gmv", "amv", " mad ", "edit",
            "compilation", "mix compilation",
        )
        official_words = (
            "official music video", "official video", "official audio",
            "official mv", "official musicvideo", "provided to youtube", " - topic",
            "visualizer", "performance video",
        )

        clean_candidates = []
        official_candidates = []

        for entry in candidates:
            haystack = " ".join([
                entry.get("title", ""),
                entry.get("channel", ""),
                entry.get("uploader", ""),
            ]).lower()

            if any(bad in haystack for bad in avoid_words):
                continue

            clean_candidates.append(entry)

            if any(good in haystack for good in official_words):
                official_candidates.append(entry)

        if official_candidates:
            return random.choice(official_candidates)

        if clean_candidates:
            return random.choice(clean_candidates)

        # 安全側: 音楽らしい候補が無ければ None にして再検索させる。
        return None

    try:
        roll = random.random()

        if roll < 0.35:
            artist = "Yvng Patra"
            result = await _direct_artist_search(artist)
            slot_name = "Yvng Patra 35%枠"

        elif roll < 0.70:
            artist = "Watson"
            result = await _direct_artist_search(artist)
            slot_name = "Watson 35%枠"

        else:
            result = await _normal_library_search()
            slot_name = "ライブラリー30%枠"

        # 直接検索枠で候補が取れなかった場合は、曲を止めず通常枠へフォールバック
        if not result and roll < 0.70:
            print(f"⚠️ [{slot_name}] 候補なし → ライブラリー枠へフォールバック")
            result = await _normal_library_search()
            slot_name = "ライブラリー30%枠（フォールバック）"

        # 履歴で候補が尽きた場合、一度だけ履歴をリセットして再検索
        if not result:
            related_history.clear()
            if roll < 0.35:
                result = await _direct_artist_search("Yvng Patra")
                slot_name = "Yvng Patra 35%枠"
            elif roll < 0.70:
                result = await _direct_artist_search("Watson")
                slot_name = "Watson 35%枠"
            else:
                result = await _normal_library_search()
                slot_name = "ライブラリー30%枠"

        if not result:
            return None

        related_history.add(result["id"])

        # 同じ曲の別動画も重複扱いにする。直近50曲ぶん保持。
        song_key = _canonical_song_key(result)
        if song_key:
            related_recent_song_keys.append(song_key)
            related_recent_song_keys = related_recent_song_keys[-50:]

        channel_key = _get_channel_key(result)
        if channel_key:
            related_recent_channels.append(channel_key)
            related_recent_channels = related_recent_channels[-5:]

        selected_title = result.get("title", "Unknown")
        print(f"🔥 [{slot_name}] 関連候補: {selected_title}")

        return {
            "url": result.get("webpage_url")
            or f"https://www.youtube.com/watch?v={result['id']}",
            "title": selected_title,
        }

    except Exception as e:
        print(f"Related Search Error: {e}")
        return None


# =========================================================
# 再生処理
# =========================================================

def _play_source(
    vc,
    audio_url,
    title,
    track,
    offset_sec=None,
    add_history=True,
    fallback_to_start=False,
):
    global last_played_track
    global resume_position_sec
    global play_started_monotonic

    start_offset = max(0.0, float(offset_sec or 0.0))

    before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

    if start_offset > 0:
        before_opts += f" -ss {start_offset:.3f}"

    ffmpeg_options = {
        "before_options": before_opts,
        "options": "-vn -loglevel error -af loudnorm=I=-16:TP=-1.5:LRA=11 -ar 48000",
    }

    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_options)

    if add_history:
        play_history.append(
            {
                "url": track["url"],
                "title": title,
            }
        )

        if len(play_history) > 50:
            del play_history[:-50]

    if vc.is_playing() or vc.is_paused():
        # ここに来る通常ケースでは直後に新しい音源を再生するため、
        # 古いafter callbackによる二重進行を防ぐ。
        skip_requested.add(vc.guild.id)
        vc.stop()

    guild_id = vc.guild.id

    def after_play(error):
        # stop() を手動で呼んだ場合は最優先で消費する。
        # VC切断との競合でもフラグを残さない。
        if guild_id in skip_requested:
            skip_requested.discard(guild_id)
            return

        if not vc.is_connected():
            return

        if error:
            print(f"Player Error: {error}")

            # 再入室時の途中シークだけは、失敗したら同じ曲を0秒から再試行
            if fallback_to_start:
                asyncio.run_coroutine_threadsafe(
                    play_resume_fallback_from_start(vc, track),
                    bot.loop,
                )
                return

        asyncio.run_coroutine_threadsafe(play_next_track(vc), bot.loop)

    last_played_track = {
        "url": track["url"],
        "title": title,
    }

    resume_position_sec = start_offset
    play_started_monotonic = time.monotonic()

    vc.play(source, after=after_play)



def save_current_position():
    """現在の再生位置をresume_position_secへ保存する。"""
    global resume_position_sec
    global play_started_monotonic

    if play_started_monotonic is None:
        return

    elapsed = max(0.0, time.monotonic() - play_started_monotonic)
    resume_position_sec = max(0.0, resume_position_sec + elapsed)
    play_started_monotonic = None


async def play_resume_fallback_from_start(vc, track):
    """途中再開が失敗した場合、同じ曲を0秒から再生する。"""
    global resume_position_sec
    global play_started_monotonic

    if not vc or not vc.is_connected():
        return False

    try:
        audio_url, title = await get_stream(track["url"])

        if not audio_url:
            print("フォールバック用音源URLを取得できませんでした。")
            await play_next_track(vc)
            return False

        _play_source(
            vc,
            audio_url,
            title,
            track,
            offset_sec=0,
            add_history=False,
            fallback_to_start=False,
        )

        print(f"↩️ [途中再開失敗 → 曲頭から再生] {title}")
        return True

    except Exception as e:
        print(f"Resume Fallback Error: {e}")

        if vc and vc.is_connected():
            await play_next_track(vc)

        return False


async def resume_last_track(vc):
    """
    退出時に保存した位置から同じ曲を再生する。
    シーク開始自体が失敗した場合も0秒再生へフォールバックする。
    """
    if not vc or not vc.is_connected() or not last_played_track:
        return False

    saved_position = max(0.0, float(resume_position_sec))

    try:
        audio_url, title = await get_stream(last_played_track["url"])

        if not audio_url:
            return await play_resume_fallback_from_start(
                vc,
                last_played_track,
            )

        try:
            _play_source(
                vc,
                audio_url,
                title,
                last_played_track,
                offset_sec=saved_position,
                add_history=False,
                fallback_to_start=saved_position > 0,
            )

            if saved_position > 0:
                print(
                    f"▶️ [続きから再開] {saved_position:.1f}秒地点: {title}"
                )
            else:
                print(f"▶️ [再開] 曲頭から: {title}")

            return True

        except Exception as e:
            print(f"途中再開開始エラー → 曲頭へフォールバック: {e}")
            return await play_resume_fallback_from_start(
                vc,
                last_played_track,
            )

    except Exception as e:
        print(f"Resume Error: {e}")
        return await play_resume_fallback_from_start(
            vc,
            last_played_track,
        )


async def play_next_track(vc):
    async with play_transition_lock:
        global playlist_cache
        global related_library
        global current_track_index
        global related_mode
    
        for _ in range(20):
            if not vc or not vc.is_connected():
                return
    
            # まだプレイリストを取得していなければ取得
            if not playlist_cache:
                playlist_cache = await get_playlist()
    
                if playlist_cache:
                    # このプレイリスト全体を関連曲参照ライブラリーとして固定
                    related_library = list(playlist_cache)
                    print(
                        f"📚 プレイリスト{selected_playlist}を関連曲ライブラリーとして読み込み: "
                        f"{len(related_library)}曲"
                    )
    
                else:
                    print("プレイリストを取得できませんでした。")
                    return
    
            # -------------------------------------------------
            # ① まずプレイリスト本編を順番に再生
            # -------------------------------------------------
            if current_track_index < len(playlist_cache):
                track = playlist_cache[current_track_index]
                current_track_index += 1
                related_mode = False
    
            # -------------------------------------------------
            # ② 全曲終了後、プレイリスト全体参照の関連曲モード
            # -------------------------------------------------
            else:
                related_mode = True
                track = await search_related_from_library()
    
                if not track:
                    print("関連曲を取得できませんでした。")
                    await asyncio.sleep(1)
                    continue
    
            audio_url, title = await get_stream(track["url"])
    
            if not audio_url:
                print(f"音源取得失敗 → スキップ: {track.get('title', 'Unknown')}")
                await asyncio.sleep(0.3)
                continue
    
            _play_source(
                vc,
                audio_url,
                title,
                track,
                offset_sec=None,
                add_history=True,
            )
    
            if related_mode:
                print(f"🔥 [ライブラリー関連曲] 再生: {title}")
            else:
                print(f"🎵 [プレイリスト本編] 再生: {title}")
    
            return
    
        print("⚠️ 再生可能な曲を20回連続で取得できませんでした。")

async def play_specific_track(vc, track):
    """戻る用。履歴に同じ曲を再追加しない。"""
    if not vc or not vc.is_connected():
        return False

    audio_url, title = await get_stream(track["url"])

    if not audio_url:
        print(f"戻る曲の音源取得失敗: {track.get('title', 'Unknown')}")
        return False

    _play_source(
        vc,
        audio_url,
        title,
        track,
        offset_sec=None,
        add_history=False,
    )

    print(f"🎵 再生（戻る）: {title}")
    return True


# =========================================================
# Bot起動
# =========================================================
@bot.event
async def on_ready():
    print(f"{bot.user.name} 起動完了！")
    print(f"discord.py: {discord.__version__}")

    # discord.py 2.7.x introduced DAVE support for voice.
    # Give a clear warning before a voice connection is attempted.
    try:
        import davey  # noqa: F401
        print("Voice dependency davey: OK")
    except ImportError:
        print(
            "⚠️ davey が見つかりません。discord.py 2.7.x のVC利用では "
            "先に `python -m pip install -U \"discord.py[voice]\"` を実行してください。"
        )


# =========================================================
# VC入退室
# =========================================================
@bot.event

async def on_voice_state_update(member, before, after):
    global play_started_monotonic

    if member.bot:
        return

    if member.name != TARGET_USER_NAME:
        return

    vc = member.guild.voice_client

    # -----------------------------------------------------
    # 対象ユーザーがVCへ入室 / 移動
    # -----------------------------------------------------
    if before.channel != after.channel and after.channel:
        try:
            if not vc or not vc.is_connected():
                vc = await after.channel.connect(
                    timeout=30.0,
                    reconnect=True,
                    self_deaf=True,
                )

            elif vc.channel != after.channel:
                await vc.move_to(after.channel)

            if not auto_join:
                return

            if vc.is_paused():
                vc.resume()
                play_started_monotonic = time.monotonic()

            elif not vc.is_playing():
                if last_played_track:
                    ok = await resume_last_track(vc)

                    if not ok:
                        await play_next_track(vc)

                else:
                    await play_next_track(vc)

        except Exception as e:
            print(f"VC Error: {e}")

    # -----------------------------------------------------
    # 対象ユーザーがVCから退出
    # -----------------------------------------------------
    elif before.channel and not after.channel:
        if vc and vc.is_connected():
            try:
                # 抜けた瞬間の再生位置を保存。
                # 一時停止中なら「おふ」の時点ですでに保存済み。
                if vc.is_playing():
                    save_current_position()

                print(
                    f"[制御] 再生位置保存: {resume_position_sec:.1f}秒"
                )

                if vc.is_playing() or vc.is_paused():
                    # stop() のafter callbackで次曲へ進ませない
                    skip_requested.add(vc.guild.id)
                    vc.stop()

                await asyncio.sleep(0.3)

                if vc.is_connected():
                    await vc.disconnect(force=True)

                print("[制御] BotもVCから退出")

            except Exception as e:
                print(f"[制御] VC退出エラー: {e}")


# =========================================================
# チャットコマンド
# =========================================================
@bot.event

async def on_message(message):
    global selected_playlist
    global playlist_cache
    global related_library
    global current_track_index
    global related_mode
    global related_history
    global related_recent_channels
    global related_recent_song_keys
    global auto_join
    global play_history
    global last_played_track
    global resume_position_sec
    global play_started_monotonic

    if message.author.bot:
        return

    content = message.content.strip().lower()
    vc = message.guild.voice_client if message.guild else None

    # -----------------------------------------------------
    # 終了 / しゅうりょう
    # GitHub ActionsでもBotプロセスを正常終了させる
    # -----------------------------------------------------
    if content in ("終了", "しゅうりょう"):
        if vc and vc.is_connected():
            try:
                if vc.is_playing() or vc.is_paused():
                    skip_requested.add(vc.guild.id)
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as e:
                print(f"終了時VC切断エラー: {e}")

        await message.channel.send("🛑 Sibyl Systemを終了します。")
        print("[制御] 終了コマンドを受信。Botを停止します。")
        await bot.close()
        return

    # -----------------------------------------------------
    # 1 / 2 / 3 = プレイリスト選択
    # -----------------------------------------------------
    if content in ("1", "2", "3"):
        target_num = int(content)

        selected_playlist = target_num
        playlist_cache = []
        related_library = []
        current_track_index = 0
        related_mode = False
        related_history.clear()
        related_recent_channels.clear()
        related_recent_song_keys.clear()
        play_history.clear()
        last_played_track = None
        resume_position_sec = 0.0
        play_started_monotonic = None

        await message.channel.send(f"🎵 プレイリスト{target_num}を選択")

        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused():
                skip_requested.add(vc.guild.id)
                vc.stop()
                await asyncio.sleep(0.2)

            await play_next_track(vc)

    # -----------------------------------------------------
    # ノー = 入室時の自動再生OFF
    # -----------------------------------------------------
    elif content in ("ノー", "のー"):
        auto_join = False
        await message.channel.send("🔕 入室時の自動再生OFF")

    # -----------------------------------------------------
    # おん = 自動再生ON + 再開
    # -----------------------------------------------------
    elif content in ("おん", "オン"):
        auto_join = True

        if vc and vc.is_connected():
            if vc.is_paused():
                vc.resume()
                play_started_monotonic = time.monotonic()
                await message.channel.send("▶️ 音楽を再開しました！")

            elif not vc.is_playing():
                if last_played_track and resume_position_sec > 0:
                    ok = await resume_last_track(vc)

                    if not ok:
                        await play_next_track(vc)
                else:
                    await play_next_track(vc)

                await message.channel.send("🎵 再生を開始しました！")

            else:
                await message.channel.send("ℹ️ すでに再生中です。")

        else:
            await message.channel.send("⚠️ BotがVCに入っていません。")

    # -----------------------------------------------------
    # おふ = 一時停止
    # -----------------------------------------------------
    elif content in ("おふ", "オフ"):
        if vc and vc.is_playing():
            # 一時停止中の時間を再生時間へ足さないよう、この時点で保存
            save_current_position()
            vc.pause()
            await message.channel.send("⏸️ 音楽を一時停止しました。")

        else:
            await message.channel.send("ℹ️ 再生中ではありません。")

    # -----------------------------------------------------
    # もどる / 戻る = ひとつ前の曲
    # -----------------------------------------------------
    elif content in ("もどる", "戻る"):
        if vc and vc.is_connected():
            if len(play_history) >= 2:
                play_history.pop()
                previous = play_history[-1]

                playlist_index = next(
                    (
                        i
                        for i, track in enumerate(playlist_cache)
                        if track.get("url") == previous.get("url")
                    ),
                    None,
                )

                if playlist_index is not None:
                    current_track_index = playlist_index + 1
                    related_mode = current_track_index >= len(playlist_cache)

                else:
                    current_track_index = len(playlist_cache)
                    related_mode = True

                if vc.is_playing() or vc.is_paused():
                    skip_requested.add(vc.guild.id)
                    vc.stop()
                    await asyncio.sleep(0.2)

                ok = await play_specific_track(vc, previous)

                if ok:
                    await message.channel.send(
                        f"↩️ 前の曲に戻りました: {previous.get('title', 'Unknown')}"
                    )
                else:
                    await message.channel.send(
                        "⚠️ 前の曲を再生できませんでした。"
                    )

            else:
                await message.channel.send("ℹ️ 戻れる曲がありません。")

        else:
            await message.channel.send("⚠️ BotがVCに入っていません。")

    # -----------------------------------------------------
    # 全部スキップ / ぜんぶすきっぷ
    # プレイリスト本編の残りを全部飛ばして関連曲モードへ
    # -----------------------------------------------------
    elif content in (
        "全部スキップ",
        "ぜんぶすきっぷ",
        "全スキップ",
        "ぜんすきっぷ",
    ):
        if vc and vc.is_connected():
            if playlist_cache:
                current_track_index = len(playlist_cache)
                related_mode = True

                if vc.is_playing() or vc.is_paused():
                    skip_requested.add(vc.guild.id)
                    vc.stop()
                    await asyncio.sleep(0.2)

                await play_next_track(vc)
                await message.channel.send(
                    "⏭️🔥 プレイリストの残りを全部スキップして関連曲モードへ移動しました！"
                )
            else:
                await message.channel.send(
                    "ℹ️ プレイリストがまだ読み込まれていません。"
                )
        else:
            await message.channel.send("⚠️ BotがVCに入っていません。")

    # -----------------------------------------------------
    # 2すきっぷ / 2スキップ / ２すきっぷ / ２スキップ
    # 現在曲 + 次の1曲を飛ばして、その次を再生
    # -----------------------------------------------------
    elif content in (
        "2すきっぷ",
        "2スキップ",
        "２すきっぷ",
        "２スキップ",
    ):
        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused():
                skip_requested.add(vc.guild.id)
                vc.stop()
                await asyncio.sleep(0.2)

                # current_track_index は現在再生中の「次」を指している。
                # プレイリスト本編ならその1曲をさらに飛ばす。
                if current_track_index < len(playlist_cache):
                    current_track_index += 1

                else:
                    # 関連曲モードでは候補を1曲取得して「飛ばした扱い」にする。
                    # related_historyにも入るので、次の選曲で同じ動画は出にくい。
                    skipped_related = await search_related_from_library()

                    if skipped_related:
                        print(
                            "⏭️ [2曲スキップで飛ばした関連曲] "
                            f"{skipped_related.get('title', 'Unknown')}"
                        )

                await play_next_track(vc)
                await message.channel.send("⏭️⏭️ 2曲スキップしました！")

            else:
                await message.channel.send("ℹ️ 再生中ではありません。")

        else:
            await message.channel.send("⚠️ BotがVCに入っていません。")

    # -----------------------------------------------------
    # スキップ / すきっぷ
    # -----------------------------------------------------
    elif content in ("スキップ", "すきっぷ"):
        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused():
                skip_requested.add(vc.guild.id)
                vc.stop()
                await asyncio.sleep(0.2)

                await play_next_track(vc)
                await message.channel.send(
                    "⏭️ 次の曲にスキップしました！"
                )

            else:
                await message.channel.send("ℹ️ 再生中ではありません。")

        else:
            await message.channel.send("⚠️ BotがVCに入っていません。")


# =========================================================
# 実行
# =========================================================
if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN が設定されていません。GitHub Actions Secretsを確認してください。"
    )

bot.run(TOKEN)
