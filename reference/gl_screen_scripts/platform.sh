#!/bin/sh

. /etc/gl_screen/scripts/common.sh

action="$1"
brightness_offset=20

if [ "$action" = "" ];then
    exit 1
fi

gen_gl_screen_config() {
    local line="$1"
    local key="${line%% *}"
    local value="${line#* }"
    local value_change=""
    if [ "$key" = "" ] || [ "$value" = "" ];then
        return
    fi
    value_change=$(sed -e "s/'/\\\\'/g")
    uci -q set gl_screen.generic.${key}="${value}"
}

if [ "$action" = "set_brightness" ];then
    brightness=$(expr "$2" + "$brightness_offset")
    echo "$brightness" > /sys/class/backlight/soc:backlight/brightness
elif [ "$action" = "set_performance" ];then
    performance="$2"
    if [ "$performance" = "1" ];then
        echo "Y" > /sys/devices/system/cpu/qcom_lpm/parameters/sleep_disabled
        echo "performance" > /sys/devices/system/cpu/cpufreq/policy0/scaling_governor
    else
        echo "N" > /sys/devices/system/cpu/qcom_lpm/parameters/sleep_disabled
        echo "schedutil" > /sys/devices/system/cpu/cpufreq/policy0/scaling_governor
    fi
elif [ "$action" = "get_backlight_state" ];then
    cat /sys/class/backlight/soc:backlight/bl_power 2>/dev/null
elif [ "$action" = "persist_config_sync" ];then
    active_config=$(flock -s /etc/gl_screen/active_config.lock -c 'cat /tmp/gl_screen/active_config 2>/dev/null')
    touch /etc/config/gl_screen
    uci -q delete gl_screen.generic
    uci -q set gl_screen.generic="cover"
    foreach_line "$active_config" "gen_gl_screen_config"
    uci -q commit gl_screen
    sync
    gen_screen_config
elif [ "$action" = "screen_active" ];then
    ubus call lpm set_screen '{"action": "active"}' 1>/dev/null 2>&1
elif [ "$action" = "kill_boot" ];then
    boot_pid=$(pidof screen_boot)
    if [ "$boot_pid" != "" ];then
        #初始化完成后关闭开机动画
        for i in `seq 1 5`
        do
            #快速结束动画
            pidof screen_boot | xargs kill -10
            sleep 1
            #进程退出
            pidof screen_boot | xargs kill -9
            sleep 1
            pidof screen_boot || break
        done
    fi
else
    exit 1
fi
