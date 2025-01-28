#!/usr/bin/env python

import os
import sys
import subprocess
import threading
import time
import signal
import argparse
from datetime import datetime
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

script_dir = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

class FileEventHandler(FileSystemEventHandler):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder
        self.last_file = None

    def on_closed(self, event):
        """处理文件关闭事件（Windows兼容）"""
        if not event.is_directory and event.src_path.lower().endswith(".mkv"):
            current_file = os.path.normpath(event.src_path)
            if current_file != self.last_file:
                with self.recorder.lock:
                    self.recorder.current_file = current_file
                    self.recorder.last_check_time = time.time()
                    self.last_file = current_file
                logging.info(f"检测到新分片文件: {current_file}")

class FFmpegRecorder:
    def __init__(self, args):
        self.args = args
        self.process = None
        self.current_file = None
        self.stop_event = threading.Event()
        self.restart_event = threading.Event()
        self.lock = threading.Lock()
        self.last_check_time = time.time()
        self.last_size = 0
        self._init_file_watcher()

    def _init_file_watcher(self):
        """初始化文件监控"""
        self.observer = Observer()
        event_handler = FileEventHandler(self)
        self.observer.schedule(
            event_handler,
            self.args.output_dir,
            recursive=False,
        )
        self.observer.start()

    def find_latest_mkv(self):
        """查找目录中最新的mkv文件"""
        output_dir = self.args.output_dir
        latest_file = None
        latest_mtime = 0
        
        try:
            for filename in os.listdir(output_dir):
                if filename.lower().endswith(".mkv"):
                    filepath = os.path.join(output_dir, filename)
                    try:
                        mtime = os.path.getmtime(filepath)
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                            latest_file = filepath
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            return None
        
        return latest_file

    def start_recording(self):
        """主录制循环"""
        while not self.stop_event.is_set():
            try:
                self._single_recording_cycle()
                
                if self.restart_event.is_set():
                    self.restart_event.clear()
                    continue
                break
                
            except Exception as e:
                logging.error(f"录制循环异常: {str(e)}")
                self.restart_event.set()

    def _single_recording_cycle(self):
        """单个录制周期"""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.current_file = os.path.join(
            self.args.output_dir, f"{timestamp}.mkv"
        )
        os.makedirs(self.args.output_dir, exist_ok=True)

        cmd = [
            self.args.ffmpeg_path,
            "-hide_banner", "-nostats" if self.args.nostats else "",
            "-rtsp_transport", self.args.rtsp_transport,
            "-i", self.args.rtsp_url,
            "-c", "copy",
            "-f", "segment", "-reset_timestamps", "1", "-strftime", "1",
            "-segment_time", str(self.args.segment_duration),
            "-segment_format", "mkv",
            os.path.join(self.args.output_dir, "%Y-%m-%d_%H-%M-%S.mkv"),
        ]

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            cwd=script_dir,
        )

        monitor_thread = threading.Thread(target=self._monitor_process)
        monitor_thread.start()

        while True:
            return_code = self.process.poll()
            
            if self.restart_event.is_set():
                logging.info("收到重启信号，终止进程中...")
                self._terminate_ffmpeg()
                break
                
            if return_code is not None:
                self._handle_exit_code(return_code)
                break
                
            if self.stop_event.is_set():
                self._terminate_ffmpeg()
                break
                
            time.sleep(1)

        monitor_thread.join(timeout=30)

    def _monitor_process(self):
        """监控文件写入状态"""
        start_time = time.time()
        file_created = False
        
        while not self.stop_event.is_set() and not self.restart_event.is_set():
            with self.lock:
                target_file = self.current_file
                last_check = self.last_check_time

            if not target_file:
                time.sleep(1)
                continue

            # 检查文件是否存在
            if not os.path.exists(target_file):
                # 尝试查找最新的mkv文件
                latest_file = self.find_latest_mkv()
                if latest_file:
                    logging.info(f"检测到更最新的文件: {latest_file}")
                    with self.lock:
                        self.current_file = latest_file
                        self.last_check_time = time.time()
                        self.last_size = os.path.getsize(latest_file)
                    continue
                else:
                    if time.time() - start_time > self.args.file_timeout:
                        logging.error("初始文件创建超时")
                        self.restart_event.set()
                    time.sleep(1)
                    continue

            # 检查文件增长
            try:
                current_size = os.path.getsize(target_file)
                if current_size > self.last_size:
                    self.last_size = current_size
                    with self.lock:
                        self.last_check_time = time.time()
                else:
                    # 检查是否有更新的文件
                    latest_file = self.find_latest_mkv()
                    if latest_file and latest_file != target_file:
                        logging.info(f"检测到更新的文件，切换至: {latest_file}")
                        with self.lock:
                            self.current_file = latest_file
                            self.last_check_time = time.time()
                            self.last_size = os.path.getsize(latest_file)
                        continue
                    elif time.time() - self.last_check_time > self.args.timeout:
                        logging.error("文件写入停滞超时")
                        self.restart_event.set()
            except FileNotFoundError:
                if file_created:
                    logging.error("文件意外消失")
                    self.restart_event.set()

            time.sleep(self.args.monitor_interval)

    def _handle_exit_code(self, code):
        """处理进程退出码"""
        if code in (255, 137):
            logging.info(f"FFmpeg正常退出，代码: {code}")
        else:
            logging.error(f"FFmpeg异常退出，代码: {code}")
            if not self.stop_event.is_set():
                self.restart_event.set()

    def _terminate_ffmpeg(self):
        """终止FFmpeg进程"""
        if self.process and self.process.poll() is None:
            try:
                # 第一阶段：优雅退出
                if self.process.stdin:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=15)
                
                if sys.platform == "win32":
                    self.process.send_signal(signal.CTRL_C_EVENT)
                else:
                    self.process.send_signal(signal.SIGINT)

                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 第二阶段：强制终止
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 第三阶段：强制杀死
                    self.process.kill()
                    self.process.wait()
            except Exception as e:
                logging.error(f"终止进程失败: {str(e)}")

    def __del__(self):
        if hasattr(self, "observer"):
            self.observer.stop()
            self.observer.join()

class CleanupManager:
    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()

    def start_cleanup(self):
        """清理线程主循环"""
        while not self.stop_event.is_set():
            try:
                self._cleanup_cycle()
                time.sleep(self.args.cleanup_interval)
            except Exception as e:
                logging.error(f"清理失败: {str(e)}")

    def _cleanup_cycle(self):
        """执行清理操作"""
        files = []
        total_size = 0

        for filename in os.listdir(self.args.output_dir):
            filepath = os.path.join(self.args.output_dir, filename)
            if not filename.lower().endswith(".mkv"):
                continue

            try:
                stat = os.stat(filepath)
                if stat.st_size == 0:  # 删除空文件
                    os.remove(filepath)
                    logging.info(f"删除空文件: {filename}")
                    continue

                file_time = datetime.fromtimestamp(stat.st_ctime)
                files.append((file_time, filepath, stat.st_size))
                total_size += stat.st_size
            except Exception as e:
                logging.warning(f"无法处理文件 {filename}: {str(e)}")

        # 按创建时间排序（旧文件在前）
        files.sort(key=lambda x: x[0])

        # 清理超过数量限制
        while len(files) > self.args.max_files:
            oldest = files.pop(0)
            try:
                os.remove(oldest[1])
                total_size -= oldest[2]
                logging.info(f"删除旧文件（数量限制）: {os.path.basename(oldest[1])}")
            except Exception as e:
                logging.error(f"删除失败 {oldest[1]}: {str(e)}")

        # 清理超过大小限制
        while total_size > self.args.max_size and files:
            oldest = files.pop(0)
            try:
                os.remove(oldest[1])
                total_size -= oldest[2]
                logging.info(f"删除旧文件（大小限制）: {os.path.basename(oldest[1])}")
            except Exception as e:
                logging.error(f"删除失败 {oldest[1]}: {str(e)}")

def signal_handler(sig, frame, recorder, cleaner):
    """处理系统信号"""
    logging.info("收到终止信号，正在关闭...")
    recorder.stop_event.set()
    cleaner.stop_event.set()
    recorder._terminate_ffmpeg()
    sys.exit(0)

def main():
    # 切换工作目录
    os.chdir(script_dir)

    parser = argparse.ArgumentParser(
        description="跨平台RTSP流媒体录像机",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    # 核心参数
    parser.add_argument("--output_dir", default="./record",
                      help="录像文件存储目录")
    parser.add_argument("--ffmpeg_path", default="./bin/ffmpeg",
                      help="FFmpeg可执行文件路径")
    parser.add_argument("--rtsp_url",
        default="rtsp://admin:password@192.168.1.2:554/stream1",
        help="RTSP流地址")
    
    # 时间参数
    parser.add_argument("--segment_duration", type=int, default=21600,
                      help="单个文件录制时长（秒）")
    parser.add_argument("--timeout", type=int, default=10,
                      help="文件写入超时时间（秒）")
    parser.add_argument("--file_timeout", type=int, default=10,
                      help="文件创建超时时间（秒）")
    parser.add_argument("--monitor_interval", type=int, default=30,
                      help="文件监控检查间隔（秒）")
    parser.add_argument("--cleanup_interval", type=int, default=3600,
                      help="清理任务执行间隔（秒）")
    
    # 存储限制
    parser.add_argument("--max_files", type=int, default=256,
                      help="最大保留文件数量")
    parser.add_argument("--max_size", type=int, default=500*1024**3,
                      help="最大存储空间（字节）")
    
    # 网络参数
    parser.add_argument("--buffer_size", type=int, default=1024000,
                      help="FFmpeg输入缓冲区大小")
    parser.add_argument("--max_delay", type=int, default=500000,
                      help="FFmpeg最大延迟微秒数")
    parser.add_argument("--stimeout", type=int, default=20000000,
                      help="FFmpeg socket超时微秒数")
    parser.add_argument("--loglevel", type=str, default="info",
                      help="FFmpeg loglevel")
    parser.add_argument("--nostats", type=bool, default=False,
                      help="FFmpeg nostats")
    parser.add_argument("--rtsp_transport", default="tcp",
                      choices=["tcp", "udp", "http"],
                      help="RTSP传输协议")

    args = parser.parse_args()

    # 路径标准化
    args.output_dir = os.path.normpath(args.output_dir)
    args.ffmpeg_path = os.path.normpath(args.ffmpeg_path)

    recorder = FFmpegRecorder(args)
    cleaner = CleanupManager(args)

    # 注册信号处理
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM,
            lambda s, f: signal_handler(s, f, recorder, cleaner))
    signal.signal(signal.SIGINT,
        lambda s, f: signal_handler(s, f, recorder, cleaner))

    # 启动清理线程
    cleanup_thread = threading.Thread(target=cleaner.start_cleanup)
    cleanup_thread.daemon = True
    cleanup_thread.start()

    try:
        recorder.start_recording()
    finally:
        recorder.stop_event.set()
        cleaner.stop_event.set()
        if hasattr(recorder, "observer"):
            recorder.observer.stop()
            recorder.observer.join()
        logging.info("服务已完全停止")

if __name__ == "__main__":
    main()