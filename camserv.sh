#!/bin/sh /etc/rc.common

START=99
STOP=15
USE_PROCD=1
PROG=/usr/bin/python
SCRIPT=/home/camera/main.py
ARGS="--rtsp_url='rtsp://...'"
WORKDIR=/home/camera
PIDFILE=/var/run/camera.pid

start_service() {
    procd_open_instance
    procd_set_param command "$PROG" "$SCRIPT" $ARGS
    procd_set_param file "$SCRIPT"
    procd_set_param working_dir "$WORKDIR"
    procd_set_param pidfile "$PIDFILE"
    procd_set_param stdout 1
    procd_set_param stderr 1
    # 设置优雅停止的超时时间（秒）
    procd_set_param term_timeout 30
    procd_set_param respawn
    procd_close_instance
}

stop_service() {
    local pid
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # 发送 SIGTERM 信号
            kill -SIGINT "$pid"
            sleep 1s
            kill -SIGINT "$pid"
            
            # 等待进程结束（最多120秒）
            local timeout=120
            while [ $timeout -gt 0 ]; do
                if ! kill -0 "$pid" 2>/dev/null; then
                    # 进程已经终止
                    rm -f "$PIDFILE"
                    return 0
                fi
                sleep 1
                timeout=$((timeout - 1))
            done
            
            # 如果超时，强制结束进程
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid"
                rm -f "$PIDFILE"
            fi
        else
            rm -f "$PIDFILE"
        fi
    fi
}

service_triggers() {
    procd_add_reload_trigger "camera"
}