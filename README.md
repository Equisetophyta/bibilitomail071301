# bibilitomail071301
一个bilibili动态直播推送至邮箱的简单实现

本项目是一个基于 `bilibili_api` 的异步脚本，用于定期检查指定 B 站用户的**动态更新**与**直播状态变化**，并通过 **SMTP 邮件通知**提醒用户：

- 当指定用户发布新动态时发送提醒邮件；
- 当指定直播间**开始或结束直播**时发送提醒邮件；
- 日志记录可用于排查运行情况。

---

## 📦 程序结构说明

| 文件/模块         | 功能描述                                                   |
|------------------|------------------------------------------------------------|
| `checkbili.py`        | 主程序，包含动态与直播检查逻辑，邮件发送与数据库存储        |
| `bilibili_api`   | 第三方库，提供 B 站用户和直播数据接口（需 pip 安装）       |
| `bili_monitor.db`| SQLite 本地数据库，记录动态和直播间状态                    |
| `bili_monitor.log`| 日志文件，记录运行日志与邮件发送状态                      |

---

## ⚙️ 配置说明（待填写的参数）

程序顶部包含需配置的常量区块如下：

```python
# ==============【邮箱 SMTP 配置】===============
SMTP_SERVER = ""         # 邮件服务器地址，例如 smtp.qq.com
SMTP_SENDER = ""         # 发件人邮箱地址
SMTP_PASSWORD = ""       # 发件人邮箱授权码或登录密码（强烈建议使用授权码）
SMTP_RECEIVERS = [""]    # 收件人列表，可以填写多个邮箱

# ==============【监控对象配置】===============
DYNAMIC_UIDS = [122345]           # 要监控动态的 B 站用户 UID 列表
LIVE_ROOM_IDS = [122345]          # 要监控直播的 B 站直播间 ID 列表

# ==============【检查间隔配置】===============
DYNAMIC_INTERVAL = 300            # 动态检查间隔（单位秒）
DYNAMIC_INTERVAL_VARIATION = 60  # 动态间隔波动范围（单位秒）
LIVE_INTERVAL = 120              # 直播检查间隔（单位秒）
LIVE_INTERVAL_VARIATION = 15     # 直播间隔波动范围（单位秒）
```
---

## 🔍 推荐关键词（用于搜索填写教程）

- SMTP 邮箱 授权码 获取方法
- Python SMTP 邮件发送 示例
- Bilibili 用户 UID 获取方式
- Bilibili 直播间 ID 查看方法
- python-dotenv 配置环境变量

---

## ✅ 示例（填写前参考）

```python
SMTP_SERVER = "smtp.qq.com"
SMTP_SENDER = "example@qq.com"
SMTP_PASSWORD = "your_auth_code"
SMTP_RECEIVERS = ["you@example.com", "friend@example.com"]

DYNAMIC_UIDS = [672328094, 12345678]
LIVE_ROOM_IDS = [12345, 67890]
```
---

# 环境安装

```shell
pip install bilibili-api-python aiosqlite structlog loguru aiosmtplib
```


# License

This project includes and depends on the following open‑source libraries:

- **bilibili-api-python (version X.Y.Z)** – licensed under **GNU General Public License v3 or later (GPL‑3.0‑or‑later)**.
- **aiosqlite** – licensed under the **MIT License**.
- **structlog** – dual‑licensed under **MIT License** or **Apache License Version 2.0** (your choice).
- **loguru** – licensed under the **MIT License**.
- **aiosmtplib** – licensed under the **MIT License**.

Project code is distributed under **[your chosen license]**.  
Please refer to the `LICENSE` file at the repository root for full license text and attribution.

第一次对于上传和协议的尝试，如有不妥欢迎指正
