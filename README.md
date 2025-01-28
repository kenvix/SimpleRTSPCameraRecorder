# Simple Camera Recorder based on FFmpeg

功能：

1. 保存到./record文件夹
2. 每个录制只持续6小时。文件名要按 yyyy-mm-dd_hh-mm-ss.mkv保存。完善基础命令，通过ffmpeg内置的方法分片
3. 监视录像文件写入情况，如果超过超时时间仍然没有变化，或文件尚未存在，认为ffmpeg出错。重启它
4. 有一个清理线程，当录制的量超过 256 个，或总大小超过 500G 时，清理超出数量的旧监控录像
5. 以上常量可通过参数传递，但具有默认值
6. 退出ffmpeg的方法是首先发送 q 等待20s，如果没响应则terminate，再等30s，如果还没响应就kill。在python接收到SIGTERM或SIGINT时应该同时关闭ffmpeg
7. 如果ffmpeg发生崩溃，即错误代码不为(0, 255, 137)，则重启ffmpeg
