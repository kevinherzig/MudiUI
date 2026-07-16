#!/bin/sh

foreach_line() {
    local fl_m=1
    local fl_next_line=""
    local fl_list="$1"
    local fl_func="$2"
    local fl_args="$3"
    local fl_line_num=$(echo "$fl_list" | sed -n "$=")
    while [ "$fl_m" -le "$fl_line_num" ];do
            fl_next_line=$(echo "$fl_list" | sed -n "${fl_m}p")
            if [ "$fl_next_line" != "" ];then
                $fl_func "$fl_next_line" "$fl_args"
            fi
            fl_m=$(expr $fl_m + 1)
    done
}

output_dir="/tmp/gl_screen/config"

foreach_line() {
    local fl_m=1
    local fl_next_line=""
    local fl_list="$1"
    local fl_func="$2"
    local fl_args="$3"
    local fl_line_num=$(echo "$fl_list" | sed -n "$=")
    while [ "$fl_m" -le "$fl_line_num" ];do
            fl_next_line=$(echo "$fl_list" | sed -n "${fl_m}p")
            if [ "$fl_next_line" != "" ];then
                $fl_func "$fl_next_line" "$fl_args"
            fi
            fl_m=$(expr $fl_m + 1)
    done
}

foreach_option() {
    local fo_option="$1"
    local fo_name="$2"
    local fo_file="${output_dir}/${fo_name}"
    local fo_key=$(echo "$fo_option" | sed -e "s/=.*//g")
    local fo_value=$(uci get gl_screen.${fo_name}.${fo_key})
    echo "$fo_key $fo_value" >> $fo_file
}

handle_option() {
    local ho_name="$1"
    local ho_option_list=$(uci -q show gl_screen.${ho_name} | grep "^gl_screen\.${ho_name}\..\+='.\+'$" | sed -e "s/^gl_screen\.${ho_name}\.\(.\+\)='\(.\+\)'$/\1=\2/g")
    foreach_line "$ho_option_list" foreach_option "$ho_name"
}

gen_screen_config() {
    local gsc_cover_list=""
    rm -rf $output_dir
    mkdir -p $output_dir
    if [ -f "/etc/config/gl_screen" ];then
        gsc_cover_list=$(uci -q show gl_screen | grep "^gl_screen.\+=cover$"  | sed -e "s/^gl_screen\.\(.\+\)=cover$/\1/g")
        foreach_line "$gsc_cover_list" handle_option
    fi
}

