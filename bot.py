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
    # runtime
    "schedule_mode": None,       # "long" or "short"
    "schedule_task": None,
    "schedule_process": None,
    "schedule_playing": False,
    "test_process": None,
    "next_play_time": None,        # datetime，等待期间下次播放的预计时间
}


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
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
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


def _start_schedule(mode: str) -> None:
    task = state["schedule_task"]
    if task and not task.done():
        task.cancel()
        kill_process(state["schedule_process"])
        state["schedule_process"] = None
        state["schedule_playing"] = False
    state["schedule_mode"] = mode
    state["next_play_time"] = None
    state["schedule_task"] = asyncio.create_task(schedule_loop(mode))


async def cmd_schedule_long(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _start_schedule("long")
    await update.message.reply_text(
        f"✅ 长间隔模式已启动\n"
        f"⏰ 间隔：{state['long_interval_minutes']} 分钟\n"
        f"🔊 播放时长：{state['long_duration_seconds']} 秒"
    )


async def cmd_schedule_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _start_schedule("short")
    await update.message.reply_text(
        f"✅ 短间隔模式已启动\n"
        f"⏰ 间隔：{format_interval(state['short_min_seconds'])}～{format_interval(state['short_max_seconds'])} 随机\n"
        f"🔊 播放时长：{state['short_duration_seconds']} 秒"
    )


async def cmd_schedule_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = state["schedule_task"]
    if not task or task.done():
        await update.message.reply_text("定时任务未在运行")
        return
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
        "  /set_random on/off — 随机播放开关\n"
        "  /set_long_interval <分钟> — 长间隔时长\n"
        "  /set_long_duration <秒> — 长间隔播放时长\n"
        "  /set_short_min <时长> — 短间隔最小值（如 3 或 30s）\n"
        "  /set_short_max <时长> — 短间隔最大值（如 10 或 90s）\n"
        "  /set_short_duration <秒> — 短间隔播放时长\n"
        "  /set_test_duration <秒> — 测试播放时长\n\n"
        "📋 /status — 查看当前状态"
    )
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = state["schedule_task"]
    running = task and not task.done()
    mode = state["schedule_mode"]
    if running and state["schedule_playing"]:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}，播放中）"
    elif running and state["next_play_time"]:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}）\n⏭ 下次播放：{format_next_play(state['next_play_time'])}"
    elif running:
        mode_label = "长间隔" if mode == "long" else "短间隔"
        schedule_str = f"运行中（{mode_label}）"
    else:
        schedule_str = "已停止"

    random_str = "开启" if state["random_enabled"] else "关闭"
    audio_str = f"{state['current_audio']}（随机）" if state["random_enabled"] else state["current_audio"]
    text = (
        "📋 当前状态\n\n"
        f"▶ 音频文件：{audio_str}\n"
        f"🔀 随机播放：{random_str}\n"
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
            BotCommand("set_random", "随机播放开关 on/off"),
            BotCommand("set_long_interval", "设置长间隔（分钟）"),
            BotCommand("set_long_duration", "设置长间隔播放时长（秒）"),
            BotCommand("set_short_min", "设置短间隔最小值（如 3、3m、30s）"),
            BotCommand("set_short_max", "设置短间隔最大值（如 10、10m、90s）"),
            BotCommand("set_short_duration", "设置短间隔播放时长（秒）"),
            BotCommand("set_test_duration", "设置测试播放时长（秒）"),
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
    app.add_handler(CommandHandler("set_long_interval", cmd_set_long_interval))
    app.add_handler(CommandHandler("set_long_duration", cmd_set_long_duration))
    app.add_handler(CommandHandler("set_short_min", cmd_set_short_min))
    app.add_handler(CommandHandler("set_short_max", cmd_set_short_max))
    app.add_handler(CommandHandler("set_short_duration", cmd_set_short_duration))
    app.add_handler(CommandHandler("set_random", cmd_set_random))
    app.add_handler(CommandHandler("set_test_duration", cmd_set_test_duration))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_select, pattern=r"^select:"))

    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
