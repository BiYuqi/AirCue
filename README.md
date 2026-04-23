# TG Audio Bot

通过 Telegram 远程控制 macOS 本地音频定时播放的 Bot。

---

## 环境要求

- macOS（依赖系统内置 `afplay`）
- Python 3.10+

---

## 安装

```bash
# 克隆/下载项目后进入目录
cd tg-audio-bot

# 创建虚拟环境
python3 -m venv venv

# 安装依赖
venv/bin/pip install -r requirements.txt

# 配置 .env
cp .env.example .env
# 编辑 .env，填入 TG_BOT_TOKEN
```

---

## 配置（.env）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TG_BOT_TOKEN` | — | BotFather 获取的 Token，必填 |
| `INTERVAL_MINUTES` | 60 | 定时播放间隔（分钟） |
| `PLAY_DURATION_SECONDS` | 60 | 定时每次播放时长（秒） |
| `TEST_DURATION_SECONDS` | 20 | 测试播放时长（秒） |
| `DEFAULT_AUDIO` | 02_audio.m4a | 默认音频文件名 |

---

## 音频文件

将音频文件放在项目根目录的 `audio/` 子目录下，命名格式：`01_audio.m4a`、`02_audio.m4a`、`03_audio.m4a`……

`/select` 命令会自动扫描 `audio/` 目录中所有符合格式的文件。

---

## 启动

```bash
bash start.sh
```

---

## 命令说明

| 命令 | 说明 |
|------|------|
| `/schedule_start` | 启动定时任务，按设定间隔循环播放 |
| `/schedule_stop` | 停止定时任务，立即中止当前播放 |
| `/test` | 测试播放当前音频（定时任务播放中则拒绝） |
| `/select` | 弹出文件列表，点击切换当前音频 |
| `/set_interval 30` | 设置定时间隔为 30 分钟，运行中立即生效 |
| `/set_duration 90` | 设置定时播放时长为 90 秒 |
| `/set_test_duration 15` | 设置测试播放时长为 15 秒 |
| `/status` | 查看当前配置和运行状态 |

---

## 停止 Bot

`Ctrl+C` 终止脚本即可，正在播放的音频会随进程退出自动停止。
