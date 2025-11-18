import asyncio
import json
import os
import random
import logging
import time
import signal
import sys
from typing import List, Optional, Dict, Any
from collections import deque
import aiosqlite
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from logging.handlers import RotatingFileHandler
import tenacity

# 尝试导入bilibili_api,如果失败则提示安装
try:
    from bilibili_api import user, live
    from bilibili_api import sync
except ImportError:
    print("错误:请先安装bilibili_api库")
    print("安装命令:pip install bilibili-api")
    sys.exit(1)


class DatabaseManager:
    """数据库管理器,处理并发安全问题"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()
    
    async def init_database(self):
        """初始化数据库表"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS dynamics (
                        uid INTEGER PRIMARY KEY, 
                        latest_timestamp INTEGER
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS lives (
                        room_id INTEGER PRIMARY KEY, 
                        live_status INTEGER, 
                        start_time INTEGER
                    )
                """)
                await db.commit()
    
    async def get_dynamic_record(self, uid: int) -> Optional[tuple]:
        """获取动态记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT latest_timestamp FROM dynamics WHERE uid=?", (uid,))
                return await cursor.fetchone()
    
    async def insert_dynamic_record(self, uid: int, latest_timestamp: int):
        """插入动态记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO dynamics (uid, latest_timestamp) VALUES (?, ?)", 
                    (uid, latest_timestamp)
                )
                await db.commit()
    
    async def update_dynamic_record(self, uid: int, latest_timestamp: int):
        """更新动态记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE dynamics SET latest_timestamp=? WHERE uid=?", 
                    (latest_timestamp, uid)
                )
                await db.commit()
    
    async def get_live_record(self, room_id: int) -> Optional[tuple]:
        """获取直播记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT live_status, start_time FROM lives WHERE room_id=?", 
                    (room_id,)
                )
                return await cursor.fetchone()
    
    async def insert_live_record(self, room_id: int, live_status: int, start_time: int):
        """插入直播记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO lives (room_id, live_status, start_time) VALUES (?, ?, ?)", 
                    (room_id, live_status, start_time)
                )
                await db.commit()
    
    async def update_live_record(self, room_id: int, live_status: int, start_time: int):
        """更新直播记录"""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE lives SET live_status=?, start_time=? WHERE room_id=?", 
                    (live_status, start_time, room_id)
                )
                await db.commit()


class EmailQueue:
    """邮件发送队列,避免频繁发送"""
    
    def __init__(self, mail_config: Dict[str, Any], logger):
        self.mail_config = mail_config
        self.logger = logger
        self.queue = deque()
        self.is_processing = False
        self._lock = asyncio.Lock()
    
    async def add_email(self, subject: str, content: str, receivers: List[str]):
        """添加邮件到发送队列"""
        async with self._lock:
            self.queue.append((subject, content, receivers))
            if not self.is_processing:
                asyncio.create_task(self._process_queue())
    
    async def _process_queue(self):
        """处理邮件发送队列"""
        async with self._lock:
            if self.is_processing:
                return
            self.is_processing = True
        
        try:
            while self.queue:
                subject, content, receivers = self.queue.popleft()
                await self._send_email_async(subject, content, receivers)
                await asyncio.sleep(2)  # 避免发送过快
        finally:
            async with self._lock:
                self.is_processing = False
    
    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=2, max=8),
        retry=tenacity.retry_if_exception_type(Exception)
    )
    async def _send_email_async(self, subject: str, content: str, receivers: List[str]):
        """异步发送邮件(带重试)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_email_sync, subject, content, receivers)
    
    def _send_email_sync(self, subject: str, content: str, receivers: List[str]) -> bool:
        """同步发送邮件"""
        try:
            message = MIMEText(content, "plain", "utf-8")
            message["From"] = Header(self.mail_config["sender"])
            message["To"] = Header(", ".join(receivers))
            message["Subject"] = Header(subject, "utf-8")
            
            with smtplib.SMTP(self.mail_config["smtp_server"], self.mail_config["port"]) as server:
                server.login(self.mail_config["sender"], self.mail_config["password"])
                server.sendmail(self.mail_config["sender"], receivers, message.as_string())
            
            self.logger.info(f"[邮件] 发送成功 | 标题:{subject} | 收件人:{', '.join(receivers)}")
            return True
        except Exception as e:
            self.logger.error(f"[邮件] 发送失败 | 错误:{e} | 标题:{subject} | 收件人:{', '.join(receivers)}")
            raise


class BiliMonitor:
    """B站监控主类"""
    
    def __init__(self):
        self.running = True
        self.tasks = []
        self.task_registry = {}  # 用于追踪任务状态
        self.logger = self._setup_logger()
        self.config = self._load_config()
        self.db_manager = DatabaseManager("bili_monitor.db")
        self.email_queue = EmailQueue(self.config["mail"], self.logger)
        
        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _setup_logger(self) -> logging.Logger:
        """设置日志记录器"""
        logger = logging.getLogger("bili_monitor")
        logger.setLevel(logging.INFO)
        
        # 避免重复添加handler
        if logger.handlers:
            return logger
        
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(message)s', 
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 控制台输出
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # 文件输出(轮转)
        try:
            file_handler = RotatingFileHandler(
                "bili_monitor.log", 
                maxBytes=5 * 1024 * 1024, 
                backupCount=3, 
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"无法创建日志文件: {e}")
        
        return logger
    
    def _load_config(self) -> Dict[str, Any]:
        """加载和验证配置文件"""
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            self.logger.error("配置文件 config.json 不存在")
            sys.exit(1)
        except json.JSONDecodeError as e:
            self.logger.error(f"配置文件格式错误: {e}")
            sys.exit(1)
        except Exception as e:
            self.logger.error(f"读取配置文件失败: {e}")
            sys.exit(1)
        
        # 验证配置文件
        if not self._validate_config(config):
            self.logger.error("配置文件验证失败")
            sys.exit(1)
        
        return config
    
    def _validate_config(self, config: Dict[str, Any]) -> bool:
        """验证配置文件格式"""
        try:
            # 验证邮件配置
            mail_required = ["smtp_server", "port", "sender", "password"]
            if "mail" not in config:
                self.logger.error("配置文件缺少 mail 配置")
                return False
            
            for key in mail_required:
                if key not in config["mail"]:
                    self.logger.error(f"邮件配置缺少必需字段: {key}")
                    return False
            
            # 验证动态监控配置
            for i, dynamic in enumerate(config.get("dynamics", [])):
                required_fields = ["uid", "interval", "receivers"]
                for field in required_fields:
                    if field not in dynamic:
                        self.logger.error(f"动态配置 {i} 缺少必需字段: {field}")
                        return False
                
                # 添加默认jitter值
                if "jitter" not in dynamic:
                    dynamic["jitter"] = 30
            
            # 验证直播监控配置
            for i, live in enumerate(config.get("lives", [])):
                required_fields = ["room_id", "interval", "receivers"]
                for field in required_fields:
                    if field not in live:
                        self.logger.error(f"直播配置 {i} 缺少必需字段: {field}")
                        return False
                
                # 添加默认jitter值
                if "jitter" not in live:
                    live["jitter"] = 30
            
            return True
        except Exception as e:
            self.logger.error(f"配置验证异常: {e}")
            return False
    
    def _signal_handler(self, signum, frame):
        """信号处理器"""
        self.logger.info(f"接收到信号 {signum},正在优雅关闭...")
        self.running = False
    
    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        retry=tenacity.retry_if_exception_type(Exception)
    )
    async def _api_call_with_retry(self, func, *args, **kwargs):
        """带重试的API调用"""
        return await func(*args, **kwargs)
    
    async def check_dynamics(self, uid: int, receivers: List[str]) -> Optional[bool]:
        """
        检查用户动态更新(基于时间戳比较最新10条动态)
        
        Args:
            uid: B站用户ID
            receivers: 邮件接收者列表
        
        Returns:
            True: 有新动态并通知成功
            False: 检查失败
            None: 无新动态
        """
        try:
            u = user.User(uid)
            page = await self._api_call_with_retry(u.get_dynamics_new)
            
            if not page.get("items"):
                self.logger.warning(f"[动态] UID {uid} 获取到空动态列表")
                return False
            
            # 获取最近10条动态
            recent_dynamics = page["items"][:10]
            if not recent_dynamics:
                self.logger.warning(f"[动态] UID {uid} 没有可用的动态数据")
                return False
            
            # 找到最新动态的时间戳
            latest_timestamp = max(
                dynamic["modules"]["module_author"]["pub_ts"] 
                for dynamic in recent_dynamics
            )
            
            # 查询数据库记录
            row = await self.db_manager.get_dynamic_record(uid)
            
            if row is None:
                # 首次记录,不发送通知
                await self.db_manager.insert_dynamic_record(uid, latest_timestamp)
                self.logger.info(f"[动态] UID {uid} 首次记录时间戳: {latest_timestamp}")
                return None
            
            stored_timestamp = row[0]
            
            # 基于时间戳判断是否有新动态
            if latest_timestamp > stored_timestamp:
                # 收集所有时间戳大于存储时间戳的新动态
                new_dynamics = [
                    d for d in recent_dynamics 
                    if d["modules"]["module_author"]["pub_ts"] > stored_timestamp
                ]
                
                if not new_dynamics:
                    self.logger.info(f"[动态] UID {uid} 时间戳更新但无新动态")
                    await self.db_manager.update_dynamic_record(uid, latest_timestamp)
                    return None
                
                # 按时间排序,最新的在前
                new_dynamics.sort(
                    key=lambda d: d["modules"]["module_author"]["pub_ts"], 
                    reverse=True
                )
                
                # 构建通知内容
                username = new_dynamics[0]["modules"]["module_author"]["name"]
                content_parts = [f"用户:{username}", f"检测到 {len(new_dynamics)} 条新动态:\n"]
                
                for i, dynamic in enumerate(new_dynamics[:5], 1):  # 最多显示5条
                    pub_ts = dynamic["modules"]["module_author"]["pub_ts"]
                    dynamic_id = dynamic["id_str"]
                    url = f"https://t.bilibili.com/{dynamic_id}"
                    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pub_ts))
                    
                    # 尝试获取动态内容概要
                    try:
                        modules = dynamic.get("modules", {})
                        module_dynamic = modules.get("module_dynamic", {})
                        desc_text = ""
                        
                        if module_dynamic:
                            desc = module_dynamic.get("desc", {})
                            if desc and "text" in desc:
                                desc_text = desc["text"][:50] + "..." if len(desc["text"]) > 50 else desc["text"]
                        
                        content_parts.append(f"{i}. 时间:{time_str}")
                        if desc_text:
                            content_parts.append(f"   内容:{desc_text}")
                        content_parts.append(f"   链接:{url}\n")
                        
                    except Exception as e:
                        self.logger.debug(f"[动态] 获取动态内容时出错: {e}")
                        content_parts.append(f"{i}. 时间:{time_str}")
                        content_parts.append(f"   链接:{url}\n")
                
                if len(new_dynamics) > 5:
                    content_parts.append(f"...还有 {len(new_dynamics) - 5} 条动态未显示")
                
                content = "\n".join(content_parts)
                
                await self.email_queue.add_email(
                    f"[B站新动态通知] {username} UID:{uid}", 
                    content, 
                    receivers
                )
                
                # 更新数据库记录为最新时间戳
                await self.db_manager.update_dynamic_record(uid, latest_timestamp)
                self.logger.info(f"[动态] UID {uid} 发现{len(new_dynamics)}条新动态,已通知")
                return True
            else:
                self.logger.info(f"[动态] UID {uid} 无新动态")
                return None
                
        except Exception as e:
            self.logger.error(f"[动态] {username} UID {uid} 获取失败 | 错误:{e}")
            return False
    
    async def check_live_status(self, room_id: int, receivers: List[str], interval: int, jitter: int) -> Optional[bool]:
        """
        检查直播状态并记录时长
        
        Args:
            room_id: 直播间ID
            receivers: 邮件接收者列表
            interval: 检查间隔
            jitter: 随机抖动
        
        Returns:
            True: 状态有变化并通知成功
            False: 检查失败
            None: 状态无变化
        """
        try:
            room = live.LiveRoom(room_id)
            info = await self._api_call_with_retry(room.get_room_info)
            live_status = info["room_info"]["live_status"]
            
            # 查询数据库记录
            row = await self.db_manager.get_live_record(room_id)
            now = int(time.time())
            
            if row is None:
                # 首次记录
                await self.db_manager.insert_live_record(room_id, live_status, 0)
                self.logger.info(f"[直播] Room {room_id} 首次记录状态:{live_status}")
                return None
            
            previous_status, start_time = row
            
            if previous_status != 1 and live_status == 1:
                # 开播
                uname = info["anchor_info"]["base_info"]["uname"]
                url = f"https://live.bilibili.com/{room_id}"
                await self.db_manager.update_live_record(room_id, 1, now)
                
                content = (
                    f"直播间:{uname}\n"
                    f"链接:{url}\n"
                    f"状态:直播开始\n"
                    f"时间:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}"
                )
                
                await self.email_queue.add_email(f"[B站开播提醒] {uname} Room:{room_id}", content, receivers)
                self.logger.info(f"[直播] Room {room_id} 检测到开播")
                return True
                
            elif previous_status == 1 and live_status != 1:
                # 下播
                uname = info["anchor_info"]["base_info"]["uname"]
                url = f"https://live.bilibili.com/{room_id}"
                duration = now - start_time if start_time else 0
                
                content = (
                    f"直播间:{uname}\n"
                    f"链接:{url}\n"
                    f"状态:直播结束\n"
                    f"开始时间:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}\n"
                    f"结束时间:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n"
                    f"时长:{duration // 60} 分钟 {duration % 60} 秒 (仅bot统计时间)"
                )
                
                await self.email_queue.add_email(f"[B站下播提醒] {uname} Room:{room_id}", content, receivers)
                await self.db_manager.update_live_record(room_id, live_status, 0)
                self.logger.info(f"[直播] Room {room_id} 检测到下播")
                return True
            else:
                self.logger.info(f"[直播] Room {room_id} 状态无变化:{live_status}")
                return None
                
        except Exception as e:
            self.logger.error(f"[直播] Room {room_id} 获取失败 | 错误:{e}")
            return False
    
    async def monitor_task(self, task_type: str, check_func, item: dict):
        """
        统一的监控任务框架(支持自动重启)
        
        Args:
            task_type: 任务类型("动态" 或 "直播")  
            check_func: 检查函数
            item: 配置项目
        """
        task_id = item.get("uid") or item.get("room_id")
        task_key = f"{task_type}_{task_id}"
        
        self.logger.info(f"[{task_type}] 开始监控任务 ID:{task_id}")
        
        consecutive_failures = 0
        max_failures = 10  # 修改为10次
        
        while self.running:
            try:
                if task_type == "动态":
                    result = await check_func(item["uid"], item["receivers"])
                else:  # 直播
                    result = await check_func(
                        item["room_id"], 
                        item["receivers"], 
                        item["interval"], 
                        item["jitter"]
                    )
                
                # 重置失败计数
                if result is not False:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    
                # 检查是否超过最大失败次数
                if consecutive_failures >= max_failures:
                    self.logger.error(f"[{task_type}] ID:{task_id} 连续失败{consecutive_failures}次,准备重启任务")
                    
                    # 发送告警邮件
                    alert_content = (
                        f"任务类型: {task_type}\n"
                        f"任务ID: {task_id}\n"
                        f"连续失败次数: {consecutive_failures}\n"
                        f"重启时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"任务将在30秒后自动重启"
                    )
                    await self.email_queue.add_email(
                        f"[监控告警] {task_type}任务重启通知", 
                        alert_content, 
                        item["receivers"]
                    )
                    
                    # 等待30秒后重启
                    self.logger.info(f"[{task_type}] ID:{task_id} 等待30秒后重启...")
                    await asyncio.sleep(30)
                    
                    # 重置失败计数并继续
                    consecutive_failures = 0
                    self.logger.info(f"[{task_type}] ID:{task_id} 任务已重启")
                    continue
                
                # 根据失败次数调整休眠时间
                if consecutive_failures >= 5:
                    extra_sleep = min(consecutive_failures * 30, 300)
                else:
                    extra_sleep = 0
                
            except Exception as e:
                consecutive_failures += 1
                self.logger.error(f"[{task_type}] ID:{task_id} 监控任务异常: {e}")
                
                # 检查异常导致的失败是否也需要重启
                if consecutive_failures >= max_failures:
                    self.logger.error(f"[{task_type}] ID:{task_id} 异常失败达到上限,准备重启")
                    await asyncio.sleep(30)
                    consecutive_failures = 0
                    continue
                    
                extra_sleep = 60  # 异常时休眠1分钟
            
            if not self.running:
                break
            
            # 计算休眠时间,确保最小间隔
            base_sleep = max(30, item["interval"] + random.randint(-item["jitter"], item["jitter"]))
            sleep_time = base_sleep + extra_sleep
            
            self.logger.debug(f"[{task_type}] ID:{task_id} 休眠 {sleep_time} 秒")
            
            # 分段休眠,便于响应停止信号
            for _ in range(sleep_time):
                if not self.running:
                    break
                await asyncio.sleep(1)
        
        self.logger.info(f"[{task_type}] 监控任务 ID:{task_id} 已停止")
    
    async def start(self):
        """启动监控程序"""
        try:
            self.logger.info("正在初始化B站监控程序...")
            
            # 初始化数据库
            await self.db_manager.init_database()
            self.logger.info("数据库初始化完成")
            
            # 创建监控任务
            self.tasks = []
            
            # 动态监控任务
            for item in self.config.get("dynamics", []):
                task = asyncio.create_task(
                    self.monitor_task("动态", self.check_dynamics, item)
                )
                self.tasks.append(task)
            
            # 直播监控任务  
            for item in self.config.get("lives", []):
                task = asyncio.create_task(
                    self.monitor_task("直播", self.check_live_status, item)
                )
                self.tasks.append(task)
            
            if not self.tasks:
                self.logger.warning("没有配置任何监控任务")
                return
            
            self.logger.info(f"已创建 {len(self.tasks)} 个监控任务")
            self.logger.info("B站监控程序启动成功,按 Ctrl+C 停止")
            
            # 等待所有任务完成
            await asyncio.gather(*self.tasks, return_exceptions=True)
            
        except Exception as e:
            self.logger.error(f"程序启动失败: {e}")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """优雅关闭程序"""
        self.logger.info("正在关闭程序...")
        
        # 取消所有任务
        for task in self.tasks:
            if not task.done():
                task.cancel()
        
        # 等待任务取消完成
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        
        # 处理剩余的邮件队列
        if hasattr(self, 'email_queue') and self.email_queue.queue:
            self.logger.info("正在发送剩余邮件...")
            while self.email_queue.queue and len(self.email_queue.queue) > 0:
                try:
                    await asyncio.wait_for(self.email_queue._process_queue(), timeout=10)
                    break
                except asyncio.TimeoutError:
                    self.logger.warning("邮件发送超时,强制退出")
                    break
        
        self.logger.info("程序已安全关闭")


async def main():
    """主程序入口"""
    # 检查依赖
    try:
        import aiosqlite
        import tenacity
    except ImportError as e:
        print(f"错误:缺少必需的依赖库: {e}")
        print("请安装所需依赖:")
        print("pip install aiosqlite tenacity")
        sys.exit(1)
    
    monitor = BiliMonitor()
    await monitor.start()


if __name__ == "__main__":
    try:
        # 兼容原程序的sync调用方式
        sync(main())
    except KeyboardInterrupt:
        print("\n程序已手动终止")
    except Exception as e:
        print(f"程序异常退出: {e}")
