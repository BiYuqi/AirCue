import asyncio
import atexit
import datetime
import logging
import os
import random
import subprocess
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

ENV_PATH = Path(__file__).parent / ".env"
AUDIO_DIR = Path(__file__).parent / "audio"
LOG_PATH = Path(__file__).parent / "play.log"

load_dotenv(ENV_PATH)


def parse_duration(arg: str) -> int | None:
    """解析时长参数，返回秒数。30s=30秒，2m=120秒，无后缀按分钟。"""
    arg = arg.strip().lower()
    if arg.endswith('s'):
        num, multiplier = arg[:-1], 1
    elif arg.endswith('m'):
        num, multiplier = arg[:-1], 60
    else:
        num, multiplier = arg, 60
    if not num.isdigit() or int(num) <= 0:
        return None
    return int(num) * multiplier


def parse_time(s: str) -> str | None:
    """解析 HH:MM 格式时间，返回规范化字符串或 None。"""
    try:
        parts = s.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except Exception:
        pass
    return None


state = {
    "current_audio": os.getenv("DEFAULT_AUDIO", "02_audio.m4a"),
    # long mode
    "long_interval_minutes": int(os.getenv("LONG_INTERVAL_MINUTES", "60")),
    "long_duration_seconds": int(os.getenv("LONG_DURATION_SECONDS", "60")),
    # short mode（内部统一秒，从 SHORT_MIN/MAX 解析，fallback 旧 _MINUTES 变量）
    "short_min_seconds": parse_duration(os.getenv("SHORT_MIN", "")) or int(os.getenv("SHORT_MIN_MINUTES", "3")) * 60,
    "short_max_seconds": parse_duration(os.getenv("SHORT_MAX", "")) or int(os.getenv("SHORT_MAX_MINUTES", "10")) * 60,
    "short_duration_seconds": int(os.getenv("SHORT_DURATION_SECONDS", "30")),
    # test
    "test_duration_seconds": int(os.getenv("TEST_DURATION_SECONDS", "20")),
    # random mode
    "random_enabled": os.getenv("RANDOM_ENABLED", "off") == "on",
    "last_played_audio": None,
    # time window
    "time_window_enabled": os.getenv("TIME_WINDOW_ENABLED", "off") == "on",
    "time_window_start": os.getenv("TIME_WINDOW_START") or None,
    "time_window_end": os.getenv("TIME_WINDOW_END") or None,
    "was_in_window": None,  # 初始化在 post_init 里，用于边缘检测
    # runtime
    "schedule_mode": None,       # "long" or "short"
    "schedule_task": None,
    "schedule_process": None,
    "schedule_playing": False,
    "test_process": None,
    "next_play_time": None,        # datetime，等待期间下次播放的预计时间
}


def in_time_window() -> bool:
    """当前时间是否在播放窗口内。未启用窗口时始终返回 True。"""
    if not state["time_window_enabled"]:
        return True
    start_str = state["time_window_start"]
    end_str = state["time_window_end"]
    if not start_str or not end_str:
        return True
    now = datetime.datetime.now().time().replace(second=0, microsecond=0)
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start = datetime.time(sh, sm)
    end = datetime.time(eh, em)
    if start <= end:
        return start <= now <= end
    else:  # 跨天，如 22:00～05:00
        return now >= start or now <= end


def kill_process(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=2)
            logging.info("kill_process: pid %s killed", proc.pid)
        except Exception as e:
            logging.warning("kill_process: kill failed (%s), falling back to pkill", e)
            _pkill_afplay()


def _pkill_afplay() -> None:
    try:
        subprocess.run(["pkill", "-x", "afplay"], capture_output=True)
    except Exception:
        pass


def kill_all_audio() -> None:
    kill_process(state.get("schedule_process"))
    kill_process(state.get("test_process"))
    state["schedule_process"] = None
    state["schedule_playing"] = False
    state["test_process"] = None


atexit.register(kill_all_audio)


def log_play(source: str, audio: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{ts}  [{source}]  {audio}\n")
    except Exception as e:
        logging.warning("log_play: write failed (%s)", e)


def format_next_play(dt: datetime.datetime) -> str:
    delta = dt - datetime.datetime.now()
    minutes = max(0, int(delta.total_seconds() / 60))
    h = dt.hour
    if h < 6:
        period = "凌晨"
    elif h < 9:
        period = "早晨"
    elif h < 12:
        period = "上午"
    elif h == 12:
        period = "中午"
    elif h < 18:
        period = "下午"
    elif h < 21:
        period = "晚上"
    else:
        period = "深夜"
    return f"{minutes} 分钟后（{period}{dt.strftime('%H:%M')}）"


def update_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            if not found:
                new_lines.append(f"{key}={value}")
                found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    tmp.rename(ENV_PATH)


def format_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分钟"


def get_audio_files() -> list[str]:
    return sorted(f.name for f in AUDIO_DIR.glob("*_audio.m4a"))


def pick_audio() -> str:
    if not state["random_enabled"]:
        return state["current_audio"]
    files = get_audio_files()
    if len(files) <= 1:
        return files[0] if files else state["current_audio"]
    last = state["last_played_audio"]
    candidates = [f for f in files if f != last]
    chosen = random.choice(candidates)
    state["last_played_audio"] = chosen
    return chosen


async def schedule_loop(mode: str) -> None:
    while True:
        state["next_play_time"] = None
        audio = pick_audio()
        filepath = AUDIO_DIR / audio
        if not filepath.exists():
            await asyncio.sleep(5)
            continue
        proc = None
        try:
            proc = subprocess.Popen(
                ["afplay", str(filepath)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            duration = (
                state["long_duration_seconds"] if mode == "long"
                else state["short_duration_seconds"]
            )
            logging.info("schedule(%s): started afplay pid=%s file=%s duration=%ss", mode, proc.pid, audio, duration)
            log_play(f"schedule_{mode}", audio)
            state["schedule_process"] = proc
            state["schedule_playing"] = True
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=duration)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error("schedule_loop: unexpected error: %s", e)
        finally:
            kill_process(proc)
            state["schedule_playing"] = False
            state["schedule_process"] = None

        if mode == "long":
            wait = state["long_interval_minutes"] * 60
        else:
            wait = random.randint(state["short_min_seconds"], state["short_max_seconds"])
        state["next_play_time"] = datetime.datetime.now() + datetime.timedelta(seconds=wait)
        logging.info("schedule(%s): next play in %ds at %s", mode, wait, state["next_play_time"].strftime("%H:%M"))
        await asyncio.sleep(wait)


def _start_schedule(mode: str) -> bool:
    """启动调度任务。返回 True=立即启动，False=窗口外已注册等待。"""
    task = state["schedule_task"]
    if task and not task.done():
        task.cancel()
        kill_process(state["schedule_process"])
        state["schedule_process"] = None
        state["schedule_playing"] = False
    state["schedule_mode"] = mode
    state["next_play_time"] = None
    if in_time_window():
        state["schedule_task"] = asyncio.create_task(schedule_loop(mode))
        return True
    else:
        state["schedule_task"] = None
        return False


async def window_watcher() -> None:
    """每 30 秒检查时间窗口边缘，触发 schedule 的自动启停。"""
    while True:
        await asyncio.sleep(30)
        if not state["time_window_enabled"]:
            state["was_in_window"] = None
            continue
        now_in = in_time_window()
        was_in = state["was_in_window"]
        if was_in is None:
            state["was_in_window"] = now_in
            continue
        if not was_in and now_in:
            # 窗口外 → 窗口内：恢复 schedule
            mode = state["schedule_mode"]
            task = state["schedule_task"]
            if mode and (not task or task.done()):
                logging.info("window_watcher: entering window, resuming schedule mode=%s", mode)
                state["schedule_task"] = asyncio.create_task(schedule_loop(mode))
                state["next_play_time"] = None
        elif was_in and not now_in:
            # 窗口内 → 窗口外：暂停 schedule，保留 mode
            logging.info("window_watcher: leaving window, suspending schedule")
            task = state["schedule_task"]
            if task and not task.done():
                task.cancel()
                state["schedule_task"] = None
            kill_process(state["schedule_process"])
            state["schedule_process"] = None
            state["schedule_playing"] = False
            state["next_play_time"] = None
        state["was_in_window"] = now_in


async def cmd_schedule_long(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = _start_schedule("long")
    if started:
        await update.message.reply_text(
            f"✅ 长间隔模式已启动\n"
            f"⏰ 间隔：{state['long_interval_minutes']} 分钟\n"
            f"🔊 播放时长：{state['long_duration_seconds']} 秒"
        )
    else:
        window_str = f"{state['time_window_start']}～{state['time_window_end']}"
        await update.message.reply_text(
            f"✅ 长间隔模式已注册\n"
            f"⏸ 当前不在播放窗口（{window_str}），进入窗口后自动开始"
        )


async def cmd_schedule_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = _start_schedule("short")
    if started:
        await update.message.reply_text(
            f"✅ 短间隔模式已启动\n"
            f"⏰ 间隔：{format_interval(state['short_min_seconds'])}～{format_interval(state['short_max_seconds'])} 随机\n"
            f"🔊 播放时长：{state['short_duration_seconds']} 秒"
        )
    else:
        window_str = f"{state['time_window_start']}～{state['time_window_end']}"
        await update.message.reply_text(
            f"✅ 短间隔模式已注册\n"
            f"⏸ 当前不在播放窗口（{window_str}），进入窗口后自动开始"
        )


async def cmd_schedule_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = state["schedule_task"]
    mode = state["schedule_mode"]
    # 窗口外待机中（mode 有值但 task 为空）也算"运行中"
    if (not task or task.done()) and not mode:
        await update.message.reply_text("定时任务未在运行")
        return
    if task and not task.done():
        task.cancel()
    state["schedule_task"] = None
    state["schedule_mode"] = None
    state["next_play_time"] = None
    kill_process(state["schedule_process"])
    state["schedule_process"] = None
    state["schedule_playing"] = False
    await update.message.reply_text("⏹ 定时任务已停止")


async def _kill_after(proc: subprocess.Popen, duration: int, state_key: str) -> None:
    await asyncio.sleep(duration)
    logging.info("_kill_after: stopping pid=%s (state_key=%s)", proc.pid, state_key)
    kill_process(proc)
    state[state_key] = None


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if state["schedule_playing"]:
        await update.message.reply_text("定时任务播放中，请稍后测试")
        return
    audio = state["current_audio"]
    filepath = AUDIO_DIR / audio
    if not filepath.exists():
        await update.message.reply_text("文件不存在，请检查 audio 目录")
        return
    try:
        proc = subprocess.Popen(
            ["afplay", str(filepath)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        state["test_process"] = proc
        logging.info("test: started afplay pid=%s file=%s duration=%ss", proc.pid, audio, state["test_duration_seconds"])
        log_play("test", audio)
        asyncio.create_task(_kill_after(proc, state["test_duration_seconds"], "test_process"))
        await update.message.reply_text(
            f"🧪 开始测试播放 {audio}，持续 {state['test_duration_seconds']} 秒"
        )
    except Exception as e:
        await update.message.reply_text(f"播放失败，请检查文件：{e}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kill_all_audio()
    task = state["schedule_task"]
    if task and not task.done():
        await update.message.reply_text("🛑 已停止当前播放（定时任务仍在运行，如需停止请用 /schedule_stop）")
    else:
        await update.message.reply_text("🛑 已停止当前播放")


async def cmd_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    files = get_audio_files()
    if not files:
        await update.message.reply_text("audio 目录下没有找到音频文件")
        return
    buttons = [
        [InlineKeyboardButton(f, callback_data=f"select:{f}")]
        for f in files
    ]
    await update.message.reply_text(
        "请选择音频文件：",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    filename = query.data.removeprefix("select:")
    state["current_audio"] = filename
    update_env("DEFAULT_AUDIO", filename)
    await query.edit_message_text(f"✅ 已切换到 {filename}")


async def cmd_set_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：\n"
            "  /set_window 08:00 18:00 — 设置播放窗口\n"
            "  /set_window off — 关闭窗口限制（全天可播）"
        )
        return

    if args[0].lower() == "off":
        state["time_window_enabled"] = False
        state["time_window_start"] = None
        state["time_window_end"] = None
        state["was_in_window"] = None
        update_env("TIME_WINDOW_ENABLED", "off")
        # 如果有待机中的 schedule，立即启动
        mode = state["schedule_mode"]
        task = state["schedule_task"]
        if mode and (not task or task.done()):
            state["schedule_task"] = asyncio.create_task(schedule_loop(mode))
            await update.message.reply_text("✅ 播放窗口已关闭（全天可播）\n▶ 已恢复定时任务运行")
        else:
            await update.message.reply_text("✅ 播放窗口已关闭（全天可播）")
        return

    if len(args) < 2:
        await update.message.reply_text("用法：/set_window 08:00 18:00")
        return

    start_str = parse_time(args[0])
    end_str = parse_time(args[1])
    if not start_str or not end_str:
        await update.message.reply_text("时间格式错误，请用 HH:MM，如 08:00 18:00")
        return
    if start_str == end_str:
        await update.message.reply_text("开始时间和结束时间不能相同")
        return

    state["time_window_enabled"] = True
    state["time_window_start"] = start_str
    state["time_window_end"] = end_str
    update_env("TIME_WINDOW_ENABLED", "on")
    update_env("TIME_WINDOW_START", start_str)
    update_env("TIME_WINDOW_END", end_str)

    currently_in = in_time_window()
    state["was_in_window"] = currently_in

    cross = "（跨天）" if start_str > end_str else ""
    window_str = f"{start_str}～{end_str}{cross}"

    if currently_in:
        await update.message.reply_text(f"✅ 播放窗口已设置：{window_str}\n当前在窗口内，定时任务不受影响")
    else:
        # 当前窗口外：暂停正在运行的 schedule
        task = state["schedule_task"]
        if task and not task.done():
            task.cancel()
            state["schedule_task"] = None
        kill_process(state["schedule_process"])
        state["schedule_process"] = None
        state["schedule_playing"] = False
        state["next_play_time"] = None
        mode = state["schedule_mode"]
        if mode:
            mode_label = "长间隔" if mode == "long" else "短间隔"
            await update.message.reply_text(
                f"✅ 播放窗口已设置：{window_str}\n"
                f"⏸ 当前窗口外，{mode_label}任务已挂起，进入窗口后自动恢复"
            )
        else:
            await update.message.reply_text(f"✅ 播放窗口已设置：{window_str}\n当前窗口外")


async def cmd_set_long_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit() or int(args[0]) <= 0:
        await update.message.reply_text("用法：/set_long_interval 60")
        return
    minutes = int(args[0])
    state["long_interval_minutes"] = minutes
    update_env("LONG_INTERVAL_MINUTES", str(minutes))
    await update.message.reply_text(f"✅ 长间隔已更新为 {minutes} 分钟")


async def cmd_set_long_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit() or int(args[0]) <= 0:
        await update.message.reply_text("用法：/set_long_duration 60")
        return
    seconds = int(args[0])
    state["long_duration_seconds"] = seconds
    update_env("LONG_DURATION_SECONDS", str(seconds))
    await update.message.reply_text(f"✅ 长间隔播放时长已更新为 {seconds} 秒")


async def cmd_set_short_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    seconds = parse_duration(args[0]) if args else None
    if not seconds:
        await update.message.reply_text("用法：/set_short_min 3（分钟）或 /set_short_min 30s（秒）")
        return
    if seconds >= state["short_max_seconds"]:
        await update.message.reply_text(f"最小值必须小于当前最大值 {format_interval(state['short_max_seconds'])}")
        return
    state["short_min_seconds"] = seconds
    update_env("SHORT_MIN", args[0])
    await update.message.reply_text(f"✅ 短间隔最小值已更新为 {format_interval(seconds)}")


async def cmd_set_short_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    seconds = parse_duration(args[0]) if args else None
    if not seconds:
        await update.message.reply_text("用法：/set_short_max 10（分钟）或 /set_short_max 90s（秒）")
        return
    if seconds <= state["short_min_seconds"]:
        await update.message.reply_text(f"最大值必须大于当前最小值 {format_interval(state['short_min_seconds'])}")
        return
    state["short_max_seconds"] = seconds
    update_env("SHORT_MAX", args[0])
    await update.message.reply_text(f"✅ 短间隔最大值已更新为 {format_interval(seconds)}")


async def cmd_set_short_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit() or int(args[0]) <= 0:
        await update.message.reply_text("用法：/set_short_duration 30")
        return
    seconds = int(args[0])
    state["short_duration_seconds"] = seconds
    update_env("SHORT_DURATION_SECONDS", str(seconds))
    await update.message.reply_text(f"✅ 短间隔播放时长已更新为 {seconds} 秒")


async def cmd_set_random(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0] not in ("on", "off"):
        await update.message.reply_text("用法：/set_random on 或 /set_random off")
        return
    enabled = args[0] == "on"
    state["random_enabled"] = enabled
    state["last_played_audio"] = None
    update_env("RANDOM_ENABLED", args[0])
    status = "已开启" if enabled else "已关闭"
    await update.message.reply_text(f"🔀 随机播放{status}")


async def cmd_set_test_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit() or int(args[0]) <= 0:
        await update.message.reply_text("用法：/set_test_duration 15")
        return
    seconds = int(args[0])
    state["test_duration_seconds"] = seconds
    update_env("TEST_DURATION_SECONDS", str(seconds))
    await update.message.reply_text(f"✅ 测试播放时长已更新为 {seconds} 秒")


async def cmd_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True,
        )
        vol = result.stdout.strip()
        await update.message.reply_text(f"🔊 当前系统音量：{vol}")
        return
    if not args[0].isdigit() or not (0 <= int(args[0]) <= 100):
        await update.message.reply_text("用法：/volume 或 /volume <0-100>")
        return
    vol = int(args[0])
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {vol}"],
        capture_output=True,
    )
    await update.message.reply_text(f"🔊 系统音量已设置为 {vol}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 命令列表\n\n"
        "▶ 调度\n"
        "  /schedule_long — 启动长间隔模式\n"
        "  /schedule_short — 启动短间隔模式\n"
        "  /schedule_stop — 停止定时任务\n\n"
        "🔊 播放\n"
        "  /test — 测试播放\n"
        "  /stop — 紧急停止所有播放\n"
        "  /select — 选择音频文件\n"
        "  /volume — 查看音量\n"
        "  /volume <0-100> — 设置音量\n\n"
        "⚙️ 配置\n"
        "  /set_window 08:00 18:00 — 设置播放时间窗口\n"
        "  /set_window off — 关闭时间窗口限制\n"
        "  /set_random on/off — 随机播放开关\n"
        "  /set_long_interval <分钟> — 长间隔时长\n"
        "  /set_long_duration <秒> — 长间隔播放时长\n"
        "  /set_short_min <时长> — 短间隔最小值（如 3 或 30s）\n"
        "  /set_short_max <时长> — 短间隔最大值（如 10 或 90s）\n"
        "  /set_short_duration <秒> — 短间隔播放时长\n"
        "  /set_test_duration <秒> — 测试播放时长\n\n"
        "📋 /status — 查看当前状态\n"
        "  /readme — 查看 audio/readme 说明"
    )
    await update.message.reply_text(text)


async def cmd_readme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    readme_path = AUDIO_DIR / "readme.txt"
    if not readme_path.exists():
        await update.message.reply_text("audio/readme 文件不存在")
        return
    text = readme_path.read_text(encoding="utf-8")
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = state["schedule_task"]
    mode = state["schedule_mode"]
    task_running = task and not task.done()

    if task_running and state["schedule_playing"]:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}，播放中）"
    elif task_running and state["next_play_time"]:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}）\n⏭ 下次播放：{format_next_play(state['next_play_time'])}"
    elif task_running:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}）"
    elif mode and state["time_window_enabled"] and not in_time_window():
        mode_label = "长间隔" if mode == "long" else "短间隔"
        window_str = f"{state['time_window_start']}～{state['time_window_end']}"
        schedule_str = f"待机中（{mode_label}，窗口外 {window_str}）"
    else:
        schedule_str = "已停止"

    # 窗口状态
    if state["time_window_enabled"] and state["time_window_start"] and state["time_window_end"]:
        cross = "（跨天）" if state["time_window_start"] > state["time_window_end"] else ""
        window_status = "窗口内 ✅" if in_time_window() else "窗口外 ⏸"
        window_line = f"⏰ 播放窗口：{state['time_window_start']}～{state['time_window_end']}{cross}（当前{window_status}）\n"
    else:
        window_line = "⏰ 播放窗口：未设置（全天可播）\n"

    random_str = "开启" if state["random_enabled"] else "关闭"
    audio_str = f"{state['current_audio']}（随机）" if state["random_enabled"] else state["current_audio"]
    text = (
        "📋 当前状态\n\n"
        f"▶ 音频文件：{audio_str}\n"
        f"🔀 随机播放：{random_str}\n"
        f"{window_line}"
        f"⏱ 定时任务：{schedule_str}\n\n"
        f"📏 长间隔模式\n"
        f"  间隔：{state['long_interval_minutes']} 分钟\n"
        f"  播放时长：{state['long_duration_seconds']} 秒\n\n"
        f"⚡ 短间隔模式\n"
        f"  间隔：{format_interval(state['short_min_seconds'])}～{format_interval(state['short_max_seconds'])} 随机\n"
        f"  播放时长：{state['short_duration_seconds']} 秒\n\n"
        f"🧪 测试时长：{state['test_duration_seconds']} 秒"
    )
    await update.message.reply_text(text)


def main() -> None:
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN not set in .env")

    _pkill_afplay()

    async def post_init(application):
        # 初始化窗口边缘检测基准值，防止启动瞬间误触发
        state["was_in_window"] = in_time_window() if state["time_window_enabled"] else None
        # 启动时间窗口监控任务
        asyncio.create_task(window_watcher())

        await application.bot.set_my_commands([
            BotCommand("help", "查看所有命令"),
            BotCommand("status", "当前配置和运行状态"),
            BotCommand("schedule_long", "启动长间隔模式（固定间隔）"),
            BotCommand("schedule_short", "启动短间隔模式（随机间隔）"),
            BotCommand("schedule_stop", "停止定时任务"),
            BotCommand("test", "一次性测试播放"),
            BotCommand("stop", "紧急停止所有音频"),
            BotCommand("select", "选择音频文件"),
            BotCommand("volume", "查看或设置系统音量"),
            BotCommand("set_window", "设置播放时间窗口"),
            BotCommand("set_random", "随机播放开关 on/off"),
            BotCommand("set_long_interval", "设置长间隔（分钟）"),
            BotCommand("set_long_duration", "设置长间隔播放时长（秒）"),
            BotCommand("set_short_min", "设置短间隔最小值（如 3、3m、30s）"),
            BotCommand("set_short_max", "设置短间隔最大值（如 10、10m、90s）"),
            BotCommand("set_short_duration", "设置短间隔播放时长（秒）"),
            BotCommand("set_test_duration", "设置测试播放时长（秒）"),
            BotCommand("readme", "查看 audio/readme.txt 说明"),
        ])

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("schedule_long", cmd_schedule_long))
    app.add_handler(CommandHandler("schedule_short", cmd_schedule_short))
    app.add_handler(CommandHandler("schedule_stop", cmd_schedule_stop))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("select", cmd_select))
    app.add_handler(CommandHandler("volume", cmd_volume))
    app.add_handler(CommandHandler("set_window", cmd_set_window))
    app.add_handler(CommandHandler("set_long_interval", cmd_set_long_interval))
    app.add_handler(CommandHandler("set_long_duration", cmd_set_long_duration))
    app.add_handler(CommandHandler("set_short_min", cmd_set_short_min))
    app.add_handler(CommandHandler("set_short_max", cmd_set_short_max))
    app.add_handler(CommandHandler("set_short_duration", cmd_set_short_duration))
    app.add_handler(CommandHandler("set_random", cmd_set_random))
    app.add_handler(CommandHandler("set_test_duration", cmd_set_test_duration))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("readme", cmd_readme))
    app.add_handler(CallbackQueryHandler(callback_select, pattern=r"^select:"))

    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
