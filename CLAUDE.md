# TG Audio Bot — CLAUDE.md

## 项目概述

一个运行在 macOS 本地的 Telegram Bot，通过 TG 命令远程控制本地音频的定时/手动播放，配置可通过 TG 修改并持久化到本地 `.env`。

---

## 技术栈

- Python 3.14+
- python-telegram-bot v21+（asyncio 版本）
- python-dotenv（读写 .env）
- subprocess（调用 afplay 播放音频）
- asyncio（定时任务）
- ffmpeg（音频格式转换，mp4 → m4a）

---

## 文件结构

```
project/
├── bot.py              # 主程序，单文件实现所有功能
├── .env                # 配置文件（Token + 运行参数，不入库）
├── .gitignore
├── requirements.txt
├── start.sh            # 启动脚本
├── stop.sh             # 停止脚本
└── audio/              # 音频文件目录（不入库）
    ├── 01_audio.m4a
    ├── 02_audio.m4a
    └── ...
```

---

## .env 配置项

```
TG_BOT_TOKEN=your_token_here

DEFAULT_AUDIO=02_audio.m4a

# 长间隔模式：固定间隔播放
LONG_INTERVAL_MINUTES=60       # 每隔多少分钟播放一次
LONG_DURATION_SECONDS=60       # 每次播放多少秒

# 短间隔模式：随机间隔播放
SHORT_MIN_MINUTES=3            # 随机间隔最小分钟数
SHORT_MAX_MINUTES=10           # 随机间隔最大分钟数
SHORT_DURATION_SECONDS=30      # 每次播放多少秒

# 测试播放时长
TEST_DURATION_SECONDS=30

# 随机播放开关（on/off）
RANDOM_ENABLED=off
```

---

## 命令列表

### 调度命令
| 命令 | 说明 |
|------|------|
| `/schedule_long` | 启动长间隔模式（固定约60分钟） |
| `/schedule_short` | 启动短间隔模式（3~10分钟随机） |
| `/schedule_stop` | 停止定时任务 |

### 播放命令
| 命令 | 说明 |
|------|------|
| `/test` | 一次性测试播放 |
| `/stop` | 紧急停止所有音频播放 |
| `/select` | 选择当前音频文件 |
| `/volume` | 查看当前系统音量 |
| `/volume <0-100>` | 设置系统音量，如 `/volume 50` |

### 配置命令
| 命令 | 说明 |
|------|------|
| `/set_random on/off` | 开关随机播放模式 |
| `/set_long_interval <分钟>` | 设置长间隔，如 `/set_long_interval 60` |
| `/set_long_duration <秒>` | 设置长间隔播放时长，如 `/set_long_duration 60` |
| `/set_short_min <分钟>` | 设置短间隔最小值，如 `/set_short_min 3` |
| `/set_short_max <分钟>` | 设置短间隔最大值，如 `/set_short_max 10` |
| `/set_short_duration <秒>` | 设置短间隔播放时长，如 `/set_short_duration 30` |
| `/set_test_duration <秒>` | 设置测试播放时长，如 `/set_test_duration 30` |
| `/status` | 查看当前配置和运行状态 |

---

## 详细功能逻辑

### 音频播放
- 使用 `afplay` 后台静默播放，`subprocess.Popen(['afplay', filepath], start_new_session=True)`
- `start_new_session=True` 确保 afplay 拿到独立音频会话，避免后台进程静默问题
- 到达时长后调用 `process.kill()` 停止，失败时用 `pkill -x afplay` 兜底
- macOS 专用，无弹窗无 UI
- bot 启动时自动清理残留 afplay 进程；`atexit` 注册退出时自动 kill

### 两种定时模式
- **长间隔模式**（`/schedule_long`）：启动后立即播放一次，之后每隔 `LONG_INTERVAL_MINUTES` 分钟固定播放
- **短间隔模式**（`/schedule_short`）：启动后立即播放一次，之后每次随机等待 `SHORT_MIN_MINUTES`～`SHORT_MAX_MINUTES` 分钟
- 两种模式互斥，启动新模式会自动停止旧模式
- `/schedule_stop` 停止当前任何模式

### 随机播放（/set_random）
- 开启后：每次播放从 `audio/` 目录随机选文件，排除上一首（不重复随机）
- 文件只有一个时不做排除限制，避免死循环
- 关闭后：播放 `/select` 选中的文件
- 长/短模式共用此开关
- 状态持久化到 `.env` 的 `RANDOM_ENABLED`

### 紧急停止（/stop）
- 强制 kill 所有正在播放的进程（定时 + 测试），并 `pkill afplay` 兜底
- 不影响定时任务调度状态（schedule_task 仍运行，下次到点继续播）

### 系统音量（/volume）
- 不带参数：查询当前系统输出音量
- 带参数 `/volume <0-100>`：通过 `osascript` 设置系统输出音量

### 测试播放（/test）
- 播放当前选中音频（不受随机模式影响，始终播放 `/select` 选中的文件）
- 持续 `TEST_DURATION_SECONDS` 秒后 kill
- 定时任务正在播放时拒绝执行

### 选择音频（/select）
- 动态扫描 `audio/` 目录，以 InlineKeyboardButton 展示
- 点击后更新内存并回写 `.env` 的 `DEFAULT_AUDIO`

### .env 回写
- 逐行替换对应 key 的值，保留其他配置和注释格式
- key 不存在则追加到文件末尾

---

## 状态管理（内存）

```python
state = {
    "current_audio": "02_audio.m4a",
    "long_interval_minutes": 60,
    "long_duration_seconds": 60,
    "short_min_minutes": 3,
    "short_max_minutes": 10,
    "short_duration_seconds": 30,
    "test_duration_seconds": 30,
    "random_enabled": False,
    "last_played_audio": None,     # 随机模式不重复用
    "schedule_mode": None,         # "long" or "short"
    "schedule_task": None,         # asyncio.Task
    "schedule_process": None,      # subprocess.Popen
    "schedule_playing": False,
    "test_process": None,          # subprocess.Popen
}
```

---

## 错误处理

- 音频文件不存在：回复「文件不存在，请检查 audio 目录」
- 参数格式错误：回复用法提示
- afplay 启动失败：捕获异常，回复「播放失败，请检查文件」
- `set_short_min/max` 校验：min 必须小于 max

---

## requirements.txt

```
python-telegram-bot==21.10
python-dotenv==1.0.0
```

---

## 实现注意事项

1. 使用 `ApplicationBuilder` 构建 bot，`application.run_polling()` 启动
2. 所有 handler 都是 async 函数
3. 定时任务用 `asyncio.create_task()` 创建，保存到 `state["schedule_task"]`
4. 播放计时用 `asyncio.create_task(_kill_after(...))` 后台 kill，handler 不阻塞（Python 3.14 限制）
5. `run_polling()` 前需调用 `asyncio.set_event_loop(asyncio.new_event_loop())`，否则 Python 3.14 报 RuntimeError
6. `subprocess.Popen` 必须加 `start_new_session=True`，否则 macOS 后台进程音频会话无声
7. `.env` 路径使用脚本所在目录的绝对路径，不依赖工作目录
8. 音频目录 = `Path(__file__).parent / "audio"`

## 音频转换

mp4 提取音频为 m4a：
```bash
ffmpeg -i input.mp4 -vn -acodec copy output.m4a
```
