import asyncio
import json
import os
import random
import sqlite3
import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from typing import Dict

from bilibili_api import user, live
from bilibili_api import sync

# 配置区域 =======================================
SMTP_SERVER = ""
SMTP_SENDER = ""
SMTP_PASSWORD = ""
SMTP_RECEIVERS = [""]

DYNAMIC_UIDS = [122345, 122345, 122345]
LIVE_ROOM_IDS = [122345, 122345, 122345]

DYNAMIC_INTERVAL = 300
DYNAMIC_INTERVAL_VARIATION = 60
LIVE_INTERVAL = 120
LIVE_INTERVAL_VARIATION = 15

DB_FILE = "bili_monitor.db"
# ===============================================

LOG_FILE = "bili_monitor.log"

logging.basicConfig(
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)


conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS dynamics (uid INTEGER PRIMARY KEY, id_str TEXT, pub_ts INTEGER)")
cursor.execute("CREATE TABLE IF NOT EXISTS lives (room_id INTEGER PRIMARY KEY, live_status INTEGER)")
conn.commit()

def send_email(subject: str, content: str):
    send_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    try:
        message = MIMEText(content, 'plain', 'utf-8')
        message['From'] = Header(SMTP_SENDER)
        message['To'] = Header(','.join(SMTP_RECEIVERS))
        message['Subject'] = Header(subject, 'utf-8')

        smtp_obj = smtplib.SMTP(SMTP_SERVER, 25)
        smtp_obj.login(SMTP_SENDER, SMTP_PASSWORD)
        smtp_obj.sendmail(SMTP_SENDER, SMTP_RECEIVERS, message.as_string())
        smtp_obj.quit()

        logging.info(f"邮件发送成功：{subject} | 发送时间：{send_time}")
    except smtplib.SMTPException as e:
        logging.error(f"邮件发送失败：{e} | 发送时间：{send_time}")


async def check_dynamics(uid: int):
    u = user.User(uid)
    offset = ""
    page = await u.get_dynamics_new(offset)
    latest = max(page["items"][:10], key=lambda d: d["modules"]["module_author"]["pub_ts"])
    id_str = latest["id_str"]
    pub_ts = latest["modules"]["module_author"]["pub_ts"]

    cursor.execute("SELECT id_str, pub_ts FROM dynamics WHERE uid=?", (uid,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute("INSERT INTO dynamics (uid, id_str, pub_ts) VALUES (?, ?, ?)", (uid, id_str, pub_ts))
        conn.commit()
        logging.info(f"[动态] UID {uid} 首次记录 id={id_str}")
        return

    if id_str != row[0]:
        url = f"https://t.bilibili.com/{id_str}"
        username = latest["modules"]["module_author"]["name"]
        content = f"用户：{username}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pub_ts))}\n链接：{url}"
        send_email(f"[B站新动态通知] UID:{uid}", content)
        cursor.execute("UPDATE dynamics SET id_str=?, pub_ts=? WHERE uid=?", (id_str, pub_ts, uid))
        conn.commit()
    else:
        logging.info(f"[动态] UID {uid} 无新动态")

async def check_live_status(room_id: int):
    room = live.LiveRoom(room_id)
    try:
        info = await room.get_room_info()
        live_status = info["room_info"]["live_status"]  # 0: 未开播, 1: 直播中, 2: 稿件轮播

        cursor.execute("SELECT live_status FROM lives WHERE room_id=?", (room_id,))
        row = cursor.fetchone()

        if row is None:
            cursor.execute("INSERT INTO lives (room_id, live_status) VALUES (?, ?)", (room_id, live_status))
            conn.commit()
            logging.info(f"[直播] Room {room_id} 首次记录状态：{live_status}")
            return

        previous_status = row[0]

        # 开播：非1 → 1
        if previous_status != 1 and live_status == 1:
            uname = info["anchor_info"]["base_info"]["uname"]
            url = f"https://live.bilibili.com/{room_id}"
            content = f"直播间：{uname}\n链接：{url}\n状态：直播开始"
            send_email(f"[B站开播提醒] Room:{room_id}", content)
            logging.info(f"[直播] Room {room_id} 检测到开播")

        # 下播：1 → 非1
        elif previous_status == 1 and live_status != 1:
            uname = info["anchor_info"]["base_info"]["uname"]
            url = f"https://live.bilibili.com/{room_id}"
            content = f"直播间：{uname}\n链接：{url}\n状态：直播结束"
            send_email(f"[B站下播提醒] Room:{room_id}", content)
            logging.info(f"[直播] Room {room_id} 检测到下播")
        else:
            logging.info(f"[直播] Room {room_id} 状态无变化：{live_status}")

        # 更新数据库
        cursor.execute("UPDATE lives SET live_status=? WHERE room_id=?", (live_status, room_id))
        conn.commit()

    except Exception as e:
        logging.error(f"[直播] Room {room_id} 获取信息失败: {e}")


async def check_dynamics_loop():
    while True:
        interval = DYNAMIC_INTERVAL + random.randint(-DYNAMIC_INTERVAL_VARIATION, DYNAMIC_INTERVAL_VARIATION)
        logging.info("[动态监控] 开始新一轮检查")
        for uid in DYNAMIC_UIDS:
            await check_dynamics(uid)
        logging.info(f"[动态监控] 本轮检查结束，休眠 {interval} 秒")
        await asyncio.sleep(interval)

async def check_live_loop():
    while True:
        interval = LIVE_INTERVAL + random.randint(-LIVE_INTERVAL_VARIATION, LIVE_INTERVAL_VARIATION)
        logging.info("[直播监控] 开始新一轮检查")
        for room_id in LIVE_ROOM_IDS:
            await check_live_status(room_id)
        logging.info(f"[直播监控] 本轮检查结束，休眠 {interval} 秒")
        await asyncio.sleep(interval)

async def main():
    await asyncio.gather(
        check_dynamics_loop(),
        check_live_loop()
    )

if __name__ == "__main__":
    try:
        sync(main())
    except KeyboardInterrupt:
        logging.info("程序已手动终止")
        conn.close()
