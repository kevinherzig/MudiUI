#!/usr/bin/env eco

local termios = require 'eco.termios'
local time = require 'eco.time'
local file = require 'eco.file'
local ubus = require 'eco.ubus'
local log = require 'eco.log'
local sys = require 'eco.sys'
local cjson = require "cjson"
local uci = require "uci"
local iwinfo = require "iwinfo"
local bufio = require 'eco.bufio'

local json_data_flag
local oled_flag
local dev
local baudrate
local read_lock_flag = 0
local mcu_status_flag = 0
--发送某些信息时加锁，进行reboot、firstboot、update等操作时禁止再次发送信息，避免错误显示
local send_msg_lock_flag = 0
local send_msg_unlock_count = 0

local flag = 0
local product_test_flag = 0
local last_msg

local temp_modem_data

local cw2217_path = "/sys/class/power_supply/cw221X-bat/"
local sgm41542s_path = "/sys/class/power_supply/charger/device/sgm41542s/"
local sgm41600_path = "/sys/class/power_supply/sgm41600-standalone/device/sgm41600/"
local aw35615_a_path = "/sys/devices/platform/soc/9c0000.qcom,qupv3_0_geni_se/998000.i2c/i2c-3/3-0022/AW35615-A/"
local aw35615_b_path = "/sys/devices/platform/soc/9c0000.qcom,qupv3_0_geni_se/980000.i2c/i2c-0/0-0022/AW35615-B/"

local temp_high = {
    enable = false,
    warned = false,
    interval = 5,
    value = 50,
}

local temp_low = {
    enable = false,
    warned = false,
    interval = 5,
    value = -10
}

local capacity = {
    enable = false,
    warned = false,
    interval = 5,
    value = 10
}

local warning = {
    temp_high = temp_high,
    temp_low = temp_low,
    capacity = capacity
}

local mcu = {
    abnormal = 0,
    abnormal_type = 0,
    temp = 0,
    capacity = 0,
    chg_state = 0,
    charge_cycle = 0,
    fastcharge = 0,
}

local battery_info = {
    driver = "",
    serial = true
}

battery_state = {
    check_num = 0,
    check_poweroff_num = 0,
    check_poweroff_flag = false,
    plugged = false,
    plugged_wait = false,
    fastcharge = false,
    low_battery_flag = false,
    critical_battery_flag = false,
    present = 0,
    present_change = 0,
    capacity = 0,
    capacity_fail_count = 0,
    capacity_cal_count = 0,
    capacity_cal_max = 0,
    current_num = 0,
    current_num_flag = -1,
    cycle_count = 0,
    temp = 0,
    voltage = 0,
    capacity_init = 0,
    capacity_init_count = 0,
    limit_voltage_flag = false,
    limit_voltage_check_count = 0,
}

local function system_shutdown()
    os.execute("/etc/init.d/umount stop")
end

--判断是否处于待机模式,将其关闭
local function clear_power_saving()
    local c = uci.cursor()
    local standby = c:get("mcu", "global", "power_saveing")
    if standby == "1" then
        c:set("wireless", "radio0", "disabled", "0")
        c:set("wireless", "radio1", "disabled", "0")
        c:set("network", "modem_1_1_2", "disabled", "0")
        c:set("mcu", "global", "power_saveing", "0")
        c:commit("wireless")
        c:commit("network")
        c:commit("mcu")
        file.sync()
    end
    c:close()
end

--是否开启待机模式，关闭wifi等
local function power_saving_mode(on)
    local c = uci.cursor()
    if on then
        print("Power saving ON\n")
        c:set("wireless", "radio0", "disabled", "1")
        c:set("wireless", "radio1", "disabled", "1")
        c:set("network", "modem_1_1_2", "disabled", "1")
        c:set("mcu", "global", "power_saveing", "1")
    else
        print("Power saving OFF\n")
        c:set("wireless", "radio0", "disabled", "0")
        c:set("wireless", "radio1", "disabled", "0")
        c:set("network", "modem_1_1_2", "disabled", "0")
        c:set("mcu", "global", "power_saveing", "0")
    end

    c:commit("wireless")
    c:commit("network")
    c:commit("mcu")
    file.sync()
    c:close()
    os.execute("/etc/init.d/network reload")
end

local function result_hander(tmp_data)
    if tmp_data:find("{\"shut_down\": \"1\"}") then
        system_shutdown()
    elseif tmp_data:find("Power_Saving_on") then
        power_saving_mode(1)
    elseif tmp_data:find("Power_Saving_off") then
        power_saving_mode()
    end
end

local function is_another_mcu_running()
    local handle = io.popen("ps | grep -v grep | grep '/usr/bin/eco /usr/bin/mcu'")
    local result = handle:read("*a")
    handle:close()

    local processes = 0
    for _ in result:gmatch("[^\r\n]+") do
        processes = processes + 1
    end

    if processes > 1 then
        return true
    else
        return false
    end
end

local lockfile = "/tmp/mcu.lock"
local function flock(filename)
    local f
    local fail_count = 0
    while(1)
    do
        f = file.open(filename, file.O_RDONLY)
        if f ~= nil then
            file.close(f)
            if fail_count > 10 and not is_another_mcu_running() then
                log.err("get lock /tmp/mcu.lock failed for 10 times and no another mcu is running, forced unlocking")
                os.remove(filename)
            else
                log.err("get lock /tmp/mcu.lock failed")
                fail_count = fail_count + 1
            end
        else
            break
        end
        time.sleep(10)
    end

    f = file.open(filename, file.O_WRONLY | file.O_CREAT)
    if f then
        file.close(f)
    end
end

local function unflock(filename)
    os.remove(filename)
end

local function read_fun(fd)
    local data = {}
    read_lock_flag = 1
    local timeout = 1
    local b = bufio.new(fd)
    while true do
        local c, err = b:read(1024, timeout)
        if not c then
            read_lock_flag = 0
            if #data > 0 then
                return table.concat(data)
            end

            return nil, err
        end

        if c then
            data[#data+1] = c
        elseif #data > 0 then
            read_lock_flag = 0
            return table.concat(data)
        end
    end
end

local function get_serial_data(dev, baudrate, data)
    local fd = file.open(dev, file.O_RDWR | file.O_NOCTTY)

    local attr, err = termios.tcgetattr(fd)
    if not attr then
        print("tcgetattr", err)
        return
    end

    local nattr = attr:clone()

    nattr:clr_flag('l', termios.ECHO)
    nattr:clr_flag('l', termios.ICANON)
    nattr:clr_flag('l', termios.ECHOE)
    nattr:clr_flag('l', termios.ISIG)

    nattr:set_cc(termios.VMIN, 0)
    nattr:set_cc(termios.VTIME, 5)
    nattr:set_speed(baudrate)

    local ok, err = termios.tcsetattr(fd, termios.TCSANOW, nattr)
    if not ok then
        print('tcsetattr:', err)
        return
    end

    if type(data) == "table" then
        data = cjson.encode(data)
    end

    --先读取串口的数据，看单片机是否发送消息过来
    local tmp_data, result
    if read_lock_flag == 0 then
        tmp_data = read_fun(fd)
    end

    if tmp_data then
        print("read tmp data====", tmp_data)
        result_hander(tmp_data)
    end

    if data ~= last_msg or flag ~= 3 or mcu_status_flag == 1 then
        print("send msg ======", data)
        mcu_status_flag = 0
        file.write(fd, data)

        local err
        if read_lock_flag == 0 then
            result, err = read_fun(fd)
        end

        if not result then
            print('reade err:', err)
        else
            print('read:', result)
        end
    end

    file.close(fd)

    return result
end

local function format_massage(buf)
    for k, _ in pairs(buf) do
        buf[k] = string.gsub(buf[k] , "\"", string.char(4))
        buf[k] = string.gsub(buf[k] , ":", string.char(3))
        buf[k] = string.gsub(buf[k], "\\", string.char(2))
        buf[k] = string.gsub(buf[k], '/', string.char(1))
    end
    local msg = cjson.encode(buf)
    local len = string.len(msg) - 1  -- 忽略末尾的 '}'
    local new = {}
    local j = 2
    new[1] = string.sub(msg, 1, 1)
    for i=2,len do
            if msg:sub(i, i) == '{' or msg:sub(i, i) == '}' then
                new[j] = string.char(string.byte(msg:sub(i, i)) - 100)
                j = j + 1
            elseif msg:sub(i, i) == '"' and msg:sub(i-1, i-1) == '\\' and msg:sub(i+1, i+1) ~= ' ' then
                new[j] = string.char(string.byte(msg:sub(i, i)) - 30)
                j = j + 1
            elseif msg:sub(i, i) == ':' then
                new[j] = ': '
                j = j + 1
            else
                new[j] = msg:sub(i, i)
                j = j + 1
            end
    end
    new[j] = '}'
    msg = table.concat(new)
    msg = string.gsub(msg , "\\u0004", string.char(4))
    msg = string.gsub(msg , "\\u0003", ":")
    msg = string.gsub(msg , "\\u0002", "\\")
    msg = string.gsub(msg , "\\u0001", '/')
    return msg
end

local function uart_write(buf)
    local msg = format_massage(buf)
    print("send mcu:",msg)
    local have_data = get_serial_data(dev, baudrate, msg)
    if have_data then
        print("data:",have_data)
    end
end

local function mcu_set_config(res)
    local c = uci.cursor()
    local mask = 0x0
    local screen = {}
    screen[0] = c:get("mcu", "global", "main_enabled")
    screen[1] = c:get("mcu", "global", "wifi_2g_enabled")
    screen[2] = c:get("mcu", "global", "wifi_5g_enabled")
    screen[3] = c:get("mcu", "global", "lan_enabled")
    screen[4] = c:get("mcu", "global", "vpn_enabled")

    for i = 0, 4 do
        if screen[i] == "1" then
            mask = mask | 1 << i
        end
    end

    res.custom_en = c:get("mcu", "global", "custom_enabled")
    local content = c:get("mcu", "global", "content")
    if content == "default" then
        res.content = " "
    else
        res.content = content
    end

    res.display_mask = string.format('%02x', mask)
    c:close()
    return res
end

local function get_ap_info(out)
    local c = uci.cursor()
    c:foreach("wireless", "wifi-iface", function(s)
        if s.mode ~= "ap" then return end
        local band = c:get("wireless",s.device,"band")
        if s.network == "lan" and band == "5g" then
            out.ssid_5g = s.ssid
            if not s.disabled or s.disabled ~= "1" then
                out.up_5g = "1"
                out.key_5g = s.key
            end
        elseif s.network == "lan" then
            out.ssid = s.ssid
            if not s.disabled or s.disabled ~= "1" then
                out.up = "1"
                out.key = s.key
            end
        end
    end)
    local hide_psk = c:get("mcu","global","wifi_password_enabled")
    out.hide_psk = hide_psk == "1" and "0" or "1"
    c:close()
    return out
end

local function get_nginx_port()
    local conf = file.readfile('/etc/nginx/conf.d/gl.conf') or ''
    local port = conf:match('listen (%d+);')
    return port and tonumber(port)
end

local function get_modem_info(out)
    local data = ubus.call('gl-session', 'call', { module = "modem", func = "get_status", params = {} })
    if data and data.result then
        if data.result.modems[1] then
            local simcard_info = data.result.modems[1].simcard or {}
            local bus = data.result.modems[1].bus or 0
            if simcard_info.status == 0 then
                local carrier_info = simcard_info.carrier or ""
                out.carrier = carrier_info:gsub("^%s*(.-)%s*$", "%1")
                if #out.carrier > 16 then
                     out.carrier = string.sub(out.carrier, 1, 16)
                end
                out.sms = tostring(data.result.new_sms_count) or "1"
                if simcard_info.signal then
                    out.signal = tostring(simcard_info.signal.strength) or "0"
                    local mode = simcard_info.signal.mode or 2
                    if mode == 2 then
                        out.modem_mode = "2G"
                    elseif mode == 3 then
                        out.modem_mode = "3G"
                    elseif mode == 4 then
                        out.modem_mode = "4G"
                    elseif mode == 41 then
                        out.modem_mode = "4G+"
                    end
                end
                if data.result.modems[1].network then
                    if data.result.modems[1].network.status and data.result.modems[1].network.status == 0 then
                        out.modem_up = "1"
                    else
                        out.modem_up = "0"
                    end
                end
                os.execute("/usr/bin/tuning_switch_control " .. bus .. " " .. 1)
            elseif simcard_info.status == 1 then
                out.SIM = "NO_REG"
                os.execute("/usr/bin/tuning_switch_control " .. bus .. " " .. 0)
            elseif simcard_info.status == 2 then
                out.SIM = "PIN_SIM"
                os.execute("/usr/bin/tuning_switch_control " .. bus .. " " .. 0)
            else
                out.SIM = "NO_SIM"
            end
        else
            out.SIM = "NO_SIM"
        end
    end
end

local function get_router_info(out)
    local c = uci.cursor()
    local mode = c:get("glconfig", "general", "mode") or "router"

    if mode == "router" then
        out.work_mode = "Router"
    end

    if mode == "wds" then
        out.work_mode = "WDS"
    end

    if mode == "ap" then
        out.work_mode = "AP"
    end

    if mode == "relay" then
        out.work_mode = "Extender"
    end

    out.lan_ip = c:get("network", "lan", "ipaddr") or "192.168.8.1"

    out.ps = c:get("mcu", "global", "power_saveing") or "0"
    c:close()
end

local function get_vpn_info(out)
    -- get all interface name from config file, avoid any weird issues
    local ifname_list_cmd = "grep -E \"config interface '(ovpnclient|wgclient)\" /etc/config/network | awk -F\"'\" '{print $2}'"
    local ifname_list_file = io.popen(ifname_list_cmd)
    if not ifname_list_file then
        return
    end

    local ifname_list = ifname_list_file:read("*a")
    ifname_list_file:close()
    if not ifname_list or ifname_list == "" then
        return
    end

    local c = uci.cursor()
    local selected_if = nil
    local vpn_ip = nil
    local first_enabled_if = nil

    for ifn in ifname_list:gmatch("[^\r\n]+") do
        ifn = ifn:match("^%s*(.-)%s*$")
        if ifn and ifn ~= "" then
            local fh = io.popen("ip addr show " .. ifn .. " 2>/dev/null | awk '/inet /{print $2; exit}'")
            if fh then
                local ip = fh:read("*l")
                fh:close()
                if ip and ip ~= "" then
                    selected_if = ifn
                    vpn_ip = ip
                    out.vpn_iface = ifn
                    out.vpn_ip = ip
                    break
                end
            end
            if not first_enabled_if and c:get("network", ifn, "disabled") == "0" then
                first_enabled_if = ifn
            end
        end
    end

    if not selected_if then
        selected_if = first_enabled_if
    end

    -- if no valid interface found, return
    if not selected_if or selected_if == "" then
        c:close()
        return
    end

    local if_en = c:get("network", selected_if, "disabled")
    if if_en == "0" then
        -- remove trailing digits to get VPN type
        local vpn_type = selected_if:gsub("%d+$", "")

        -- vpn type mapping
        local vpn_type_map = {
            wgclient = "wireguard"
        }
        out.vpn_type = vpn_type_map[vpn_type] or vpn_type

        local vpn_id = c:get("network", selected_if, "config")
        local vpn_name = vpn_id and c:get(out.vpn_type, vpn_id, "name")
        out.vpn_server = vpn_name or " "

        if vpn_ip and vpn_ip ~= "" then
            out.vpn_status = "connected"
        else
            out.vpn_status = "connecting"
        end
    end

    c:close()
end

local function get_clients_info(out)
    local r = ubus.call("gl-clients", "status", {}) or {cable_total = 0, wireless_total = 0}
    local online_client_number = r.cable_total + r.wireless_total
    out.clients = tostring(online_client_number) or "0"
end

local function get_system_time(out)
    local dnsmasqsec = file.access("/var/state/dnsmasqsec")
    if dnsmasqsec then
        local date = os.date("%H:%M");
        out.clock = date or "unsync"
    else
        out.clock = "unsync"
    end
end

local function get_network_meth(out)
    local c = uci.cursor()
    local tor_enable = c:get("tor", "global", "enable") or "0"
    if tor_enable == "1" then
        if file.access("/var/lib/tor/control.log") then
            local rs_file = assert(io.popen("cat /var/lib/tor/control.log |grep Bootstrapped|tail -n 1|awk -F \"[ :]\" '{print $9}'"))
            local tor_con_percent = rs_file:read()
            rs_file:close()
            if tor_con_percent then
                if tor_con_percent:find("100%%") then
                    out.tor = "1"
                elseif tor_con_percent:find("%%") then
                    out.method_nw = "Tor booting " .. tor_con_percent
                    c:close()
                    return
                end
            end
        end
    end

    local function get_egress_interface()
        local stdout = sys.sh('ip route get 8.8.8.8')
        return stdout and stdout:match("dev (%S+)")
    end

    local function find_logical_interface(physical_dev)
        if file.access("/proc/gl-kmwan/status") then
            for line in io.lines("/proc/gl-kmwan/status") do
                local iftype, netdev = line:match("^(%S+)%s+(%S+)%s+")
                if netdev == physical_dev then
                    return iftype
                end
            end
        end
        return nil
    end

    local physical_dev = get_egress_interface()
    if not physical_dev then
        physical_dev = "unknown"
    end

    local logical_if = find_logical_interface(physical_dev)
    if not logical_if then
        logical_if = "unknown"
    end

    if file.access("/tmp/run/mwan3/indicator") then
        local cur_meth = file.readfile('/tmp/run/mwan3/indicator', "*l") or ""
        local meth_status = "/tmp/run/mwan3/iface_state/" .. cur_meth
        if file.access(meth_status) then
            local cur_status = file.readfile(meth_status, "*l") or ""
            if cur_status == "online" then
                if cur_meth == "modem_1_1_2" then
                    out.method_nw = "modem"
                elseif cur_meth == "wwan" then
                    local s = iwinfo.info("wlan-sta0")
                    if s and  type(s) == "table" and s.ssid then
                        if #s.ssid > 16 then
                            s.ssid = string.sub(s.ssid, 1, 16)
                        end
                        out.method_nw = "repeater|" .. s.ssid
                    end
                elseif cur_meth == "wan" then
                    local c = uci.cursor()
                    local proto = c:get("network", "wan", "proto")
                    if proto then
                        out.method_nw = "cable|" .. proto
                    else
                        out.method_nw = "cable"
                    end
                elseif cur_meth == "tethering" then
                    out.method_nw = "tethering"
                end
            end
        end
    else
        if logical_if == "modem_1_1_2" then
            out.method_nw = "modem"
        elseif logical_if == "wwan" then
            local s = iwinfo.info(physical_dev)
            if s and  type(s) == "table" and s.ssid then
                if #s.ssid > 16 then
                    s.ssid = string.sub(s.ssid, 1, 16)
                end
                out.method_nw = "repeater|" .. s.ssid
            end
        elseif logical_if == "wan" then
            local c = uci.cursor()
            local proto = c:get("network", "wan", "proto")
            if proto then
                out.method_nw = "cable|" .. proto
            else
                out.method_nw = "cable"
            end
        elseif logical_if == "simo" then
            local simo = c:get("network", "simo", "ifname") or ""
            if simo ~= "" and simo == "simonet" then
                out.method_nw = "modem"
            else
                out.method_nw = "simo"
            end
        elseif logical_if == "tethering" then
            out.method_nw = "tethering"
        end
    end
    c:close()
end

local function get_disk_status(out)
    out.disk = "0"
    if file.access("/proc/mounts") then
        for l in io.lines("/proc/mounts") do
            if (l:match("/dev/sd") and l:match("/tmp/mountd/")) or (l:match("/dev/sd") and l:match("/mnt/")) then
                out.disk = "1"
            end
        end
    end
end

local function get_info()
    local res = {}
    print("function get_info")
    get_ap_info(res)
    get_vpn_info(res)
    get_router_info(res)
    get_modem_info(res)
    get_clients_info(res)
    get_system_time(res)
    get_network_meth(res)
    get_disk_status(res)
    return res
end

local function timer_monitor()
    local res = get_info()
    res.mcu_status = "1"
    if flag < 3 then
        res = mcu_set_config(res)
        flag = flag + 1
    end
    local msg = format_massage(res)
    --print("send msg ======", msg)
    local have_data = msg and get_serial_data(dev, baudrate, msg)
    if have_data then
        print("mcu_msg",have_data)
        local check_ok = string.sub(have_data,0,4)
        if check_ok == "{OK}" then
            print("check OK")
            local capacity, temp, chg_state, charge_cycle = have_data:match('(%d+),(-?[%d%.]+),(%d+),*(%d*)')
            if temp and tonumber(temp) > 100 then
                temp = tostring(temp / 10)
            end

            if temp and temp == "-273.1" then
                temp = "0"
            end

            mcu.temp = temp or mcu.temp
            mcu.capacity = capacity or mcu.capacity
            mcu.chg_state = chg_state or mcu.chg_state
            mcu.charge_cycle = charge_cycle and charge_cycle ~= "" and  charge_cycle or nil
        end
        last_msg = msg
    end
end

local function warning_temp_high(temp)
    if warning.temp_high.enable then
        if temp <= (warning.temp_high.value - warning.temp_high.interval) then
            warning.temp_high.warned = false
        end
        if temp >= warning.temp_high.value and warning.temp_high.warned == false then
            if not ubus.call("gl-cloud", "subscribe_events_notify", {type = "system/high_temp", qos = 2, data = { value = temp }}) then
                warning.temp_high.warned = false
                log.err("send warning high temp :", temp, " failed")
            else
                warning.temp_high.warned = true
                log.info("warning high temp :", temp)
            end
        end
    end
end

local function warning_temp_low(temp)
    if warning.temp_low.enable then
        if temp >= (warning.temp_low.value + warning.temp_low.interval) then
            warning.temp_low.warned = false
        end
        if temp <= warning.temp_low.value and warning.temp_low.warned == false then
            if not ubus.call("gl-cloud", "subscribe_events_notify", {type = "system/low_temp", qos = 2, data = { value = temp }}) then
                warning.temp_low.warned = false
                log.err("send warning low temp :", temp, " failed")
            else
                warning.temp_low.warned = true
                log.info("warning low temp :", temp)
            end
        end
    end
end

local function warning_capacity(capacity)
    if warning.capacity.enable then
        if capacity >= (warning.capacity.value + warning.capacity.interval) then
            warning.capacity.warned = false
        end
        if capacity <= warning.capacity.value and warning.capacity.warned == false then
            if not ubus.call("gl-cloud", "subscribe_events_notify", {type = "system/low_capacity", qos = 2, data = { value = capacity }}) then
                warning.capacity.warned = false
                log.err("send warning capacity :", capacity, " failed")
            else
                warning.capacity.warned = true
                log.info("warning capacity :", capacity)
            end
        end
    end
end

local function read_capacity()
    local capacity = 0
    local soc_stretch_start = 50
    local soc_full_start = 85
    if file.access(cw2217_path .. "capacity") then
        capacity = tonumber(file.readfile(cw2217_path .. "capacity", "*l")) or 0
    end

    if capacity < 0 then capacity = 0 end
    if capacity > 100 then capacity = 100 end

    if capacity >= soc_full_start then
        capacity = 100
        return capacity
    end

    if capacity >= soc_stretch_start then
        tmp_capacity = (100 - soc_stretch_start) * (capacity - soc_stretch_start) / (soc_full_start - soc_stretch_start) + soc_stretch_start
        tmp_capacity = math.floor(tmp_capacity)
        if tmp_capacity < 0 then tmp_capacity = 0 end
        if tmp_capacity > 100 then tmp_capacity = 100 end
        return tmp_capacity
    end

    return capacity
end

local function read_present()
    local present = 0
    if file.access(cw2217_path .. "present") then
        present = tonumber(file.readfile(cw2217_path .. "present", "*l")) or 0
    end
    return present
end

local function read_temp()
    local temp = 0
    if file.access(cw2217_path .. "temp") then
        temp = tonumber(file.readfile(cw2217_path .. "temp", "*l")) or 0
    end
    return temp
end

local function read_cycle_count()
    local cycle_count = 0
    if file.access(cw2217_path .. "cycle_count") then
        cycle_count = tonumber(file.readfile(cw2217_path .. "cycle_count", "*l")) or 0
    end
    return cycle_count
end

local function read_sgm41542s_vbus()
    local vbus = 0
    if file.access(sgm41542s_path .. "vbus_adc") then
        vbus = tonumber(file.readfile(sgm41542s_path .. "vbus_adc", "*l")) or 0
    end
    return vbus
end

local function read_sgm41600_vbus()
    local vbus = 0
    if file.access(sgm41600_path .. "vbus_adc") then
        vbus = tonumber(file.readfile(sgm41600_path .. "vbus_adc", "*l")) or 0
    end
    return vbus
end

local function read_sgm41542s_ibat()
    local ibat = 0
    if file.access(sgm41542s_path .. "ibat_adc") then
        ibat = tonumber(file.readfile(sgm41542s_path .. "ibat_adc", "*l")) or 0
    end
    return ibat
end

local function read_sgm41542s_vbat()
    local vbat = 0
    if file.access(sgm41542s_path .. "vbat_adc") then
        vbat = tonumber(file.readfile(sgm41542s_path .. "vbat_adc", "*l")) or 0
    end
    return vbat
end

local function read_sgm41600_ibat()
    local ibat = 0
    if file.access(sgm41600_path .. "ibat_adc") then
        ibat = tonumber(file.readfile(sgm41600_path .. "ibat_adc", "*l")) or 0
    end
    return ibat
end

local function read_sgm41600_vbat()
    local vbat = 4400
    if file.access(sgm41600_path .. "vbat_adc") then
        vbat = tonumber(file.readfile(sgm41600_path .. "vbat_adc", "*l")) or 4400
    end
    return vbat
end

local function read_voltage_now()
    local voltage = 4400000
    if file.access(cw2217_path .. "voltage_now") then
        voltage = tonumber(file.readfile(cw2217_path .. "voltage_now", "*l")) or 4400000
    end
    return voltage
end

local function read_current_now()
    local current = 0
    if file.access(cw2217_path .. "current_now") then
        current = tonumber(file.readfile(cw2217_path .. "current_now", "*l")) or 0
    end
    return current
end

local function read_vreg()
    local vreg = 0
    if file.access(sgm41542s_path .. "vreg") then
        vreg = tonumber(file.readfile(sgm41542s_path .. "vreg", "*l")) or 4400000
    end
    return vreg
end

local function read_aw35615_pwr()
    if file.access(aw35615_a_path .. "cc_pin") then
        local cc_pin_a = file.readfile(aw35615_a_path .. "cc_pin", "*l") or "None"
        if cc_pin_a ~= "None" then
            if file.access(aw35615_a_path .. "pwr_role") then
                local pwr_a = file.readfile(aw35615_a_path .. "pwr_role", "*l")
                if pwr_a and pwr_a == "Sink" then
                    return 1
                end
            end
        end
    end

    if file.access(aw35615_b_path .. "cc_pin") then
        local cc_pin_b = file.readfile(aw35615_b_path .. "cc_pin", "*l") or "None"
        if cc_pin_b ~= "None" then
            if file.access(aw35615_b_path .. "pwr_role") then
                local pwr_b = file.readfile(aw35615_b_path .. "pwr_role", "*l")
                if pwr_b and pwr_b == "Sink" then
                    return 1
                end
            end
        end
    end

    return 0
end

local check_poweroff_timer = time.timer(function(tmr)
    if not battery_state.check_poweroff_flag then
        return
    end

    local current_now = read_current_now()
    local capacity = read_capacity()

    if battery_state.present == 0 or current_now >= 0 or capacity > 0 or battery_state.capacity > 0 then
        battery_state.check_poweroff_flag = false
        battery_state.check_poweroff_num = 0
    end

    if battery_state.present == 1 and capacity <= 0 and battery_state.capacity <= 0 then
        if battery_state.check_poweroff_num >= 30 then
            os.execute('echo "glinet battery error poweroff" > /dev/ttyMSM0')
            if file.access("/proc/gl-hw-info/screen") and file.access("/etc/gl_screen/scripts/gl_screen_event.lua") then
                local f = io.popen("lua /etc/gl_screen/scripts/gl_screen_event.lua get_screen_poweroff_anim_time")
                local out = f:read("*a"):match("%S+")
                local time_num = tonumber(out)
                if time_num then
                    delay = time_num
                    ubus.call("gl_screen", "set", {method = "poweroff anim start"})
                    if delay then
                        time.sleep(delay)
                    end
                end
            end
            os.execute('poweroff')
        else
            battery_state.check_poweroff_num = battery_state.check_poweroff_num + 1
            tmr:set(1)
        end
    else
        battery_state.check_poweroff_flag = false
        battery_state.check_poweroff_num = 0
    end

end)

local update_battery_timer = time.timer(function(tmr)
    if battery_state.busy then
        tmr:set(1)
        return
    end

    battery_state.busy = true
    local plugged = 0
    local local_fastcharge = false
    local capacity_update_flag = false
    local present = read_present()
    local sgm41542s_vbat = read_sgm41542s_vbat()
    local sgm41600_vbat = read_sgm41600_vbat()
    local sgm41542s_vbus = read_sgm41542s_vbus()
    local sgm41600_vbus = read_sgm41600_vbus()
    local cw2217_voltage = read_voltage_now()
    local temp = read_temp()
    local current_now = read_current_now()

    if battery_state.present ~= present then
        battery_state.present = present
        if present == 1 then
            if temp == -400 then
                battery_state.present = 0
                tmr:set(3)
            else
                local capacity = read_capacity()
                battery_state.temp = tostring(temp / 10)
                if (battery_state.capacity - capacity) < 2 and capacity ~= 100 and capacity ~= 0 then
                    battery_state.capacity = capacity
                    capacity_update_flag = true
                end

                if battery_state.capacity_init == 0 then
		    battery_state.capacity = capacity
		    battery_state.capacity_init = 1
                    if capacity ~= 0 and capacity ~= 100 then
                        capacity_update_flag = true
                    end
                end

                if battery_state.capacity > 100 then
                    capacity_update_flag = true
                    battery_state.capacity = 100
                end

                if battery_state.capacity < 0 then
                    capacity_update_flag = true
                    battery_state.capacity = 0
                end

                if capacity_update_flag then
                    mcu.capacity = battery_state.capacity
                end
                mcu.temp = battery_state.temp
            end
        end
        ubus.call("gl_screen", "set", {method = "battery_status_change"})
    end

    if sgm41542s_vbus > 3000 or sgm41600_vbus > 3000 then
        plugged = 1
        if sgm41542s_vbus > 7000 or sgm41600_vbus > 7000 then
            local_fastcharge = true
        end
    end

    if battery_state.plugged ~= plugged then
        battery_state.plugged = plugged
        battery_state.capacity_cal_count = 0
        battery_state.capacity_fail_count = 0
        if plugged then
            if battery_state.check_poweroff_flag then
                battery_state.check_poweroff_flag = false
                check_poweroff_timer:cancel()
            end
            ubus.call("gl_screen", "set", {method = "battery_status_change"})
            battery_state.check_num = 0
            battery_state.plugged_wait = true
        else
            battery_state.plugged_wait = false
            battery_state.fastcharge = false
        end
    end

    if battery_state.fastcharge ~= local_fastcharge then
        battery_state.fastcharge = local_fastcharge
        if battery_state.fastcharge then
            ubus.call("gl_screen", "set", {method = "battery_status_change"})
        end
    end

    if battery_state.check_num < 50 and battery_state.plugged_wait then
        battery_state.check_num = battery_state.check_num + 1
        tmr:set(0.2)
    else
        battery_state.plugged_wait = false
    end

    if (temp < 3 or temp >= 570) and current_now <= 0 then
        mcu.chg_state = 0
    else
        mcu.chg_state = battery_state.plugged
    end

    mcu.fastcharge = battery_state.fastcharge
    if battery_state.present == 0 then
        mcu.abnormal = 1
        mcu.abnormal_type = 1
    end

    if cw2217_voltage > 4500000 then
        mcu.abnormal = 1
        mcu.abnormal_type = 2
    end

    if present == 1 and battery_state.capacity == 100 and current_now < 4000000 then
        mcu.abnormal = 1
        mcu.abnormal_type = 2
    end

    if battery_state.present == 1 then
        local pwr_sink = read_aw35615_pwr()
        if pwr_sink ~= 1 then
            battery_state.limit_voltage_check_count = 0
            if battery_state.limit_voltage_flag then
                battery_state.limit_voltage_flag = false
                ubus.call("gl_screen", "set", {method = "charging recovery"})
            end
        end

        if battery_state.capacity_init == 0 then
            mcu.abnormal = 1
            mcu.abnormal_type = 1
        else
            mcu.abnormal = 0
            mcu.abnormal_type = 0
        end
    end
    battery_state.busy = false
end)

local function read_cw2217_state()
    if battery_state.busy or battery_state.present == 0 then
        return
    end

    battery_state.read_cw2217_flag = true
    local capacity = read_capacity()
    local temp = read_temp()
    local cycle_count = read_cycle_count()
    local cw2217_voltage = read_voltage_now()
    local sgm41542s_vbat = read_sgm41542s_vbat()
    local sgm41600_vbat = read_sgm41600_vbat()
    local sgm41542s_vbus = read_sgm41542s_vbus()
    local sgm41600_vbus = read_sgm41600_vbus()
    local sgm41542s_ibat = read_sgm41542s_ibat()
    local sgm41600_ibat = read_sgm41600_ibat()
    local current_now = read_current_now()
    local vreg = read_vreg()

    if battery_state.present == 1 then
        local cap_offset = capacity - battery_state.capacity
        if capacity <= 10 then
            battery_state.capacity_cal_max = 3
        else
            battery_state.capacity_cal_max = 4
        end

        if battery_state.capacity_init == 0 then
            battery_state.capacity = capacity
            battery_state.capacity_init = 1
        end

        if math.abs(cap_offset) <= 10 then
            if cap_offset < 0 then
                if battery_state.plugged == 1 then
                    if battery_state.capacity_cal_count >= battery_state.capacity_cal_max then
                        if battery_state.current_num > 0 then
                            battery_state.capacity = battery_state.capacity
                        else
                            battery_state.capacity = battery_state.capacity - 1
                        end
                        battery_state.capacity_init = 1
                        battery_state.current_num = 0
                        battery_state.capacity_cal_count = 0
                    else
                        battery_state.capacity_cal_count = battery_state.capacity_cal_count + 1
                        if battery_state.current_num_flag == 0 then
                            battery_state.current_num = 0
                        end
                        battery_state.current_num_flag = 1
                        battery_state.current_num = battery_state.current_num + current_now
                    end
                else
                    if battery_state.capacity_cal_count >= battery_state.capacity_cal_max then
                        battery_state.capacity = battery_state.capacity - 1
                        battery_state.capacity_cal_count = 0
                        if battery_state.current_num > 0 then
                            battery_state.capacity = battery_state.capacity + 1
                        end
                        battery_state.capacity_init = 1
                        battery_state.current_num = 0
                    else
                        battery_state.capacity_cal_count = battery_state.capacity_cal_count + 1
                        if battery_state.current_num_flag == 1 then
                            battery_state.current_num = 0
                        end
                        battery_state.current_num_flag = 0
                        battery_state.current_num = battery_state.current_num + current_now
                    end
                end
            end

            if cap_offset > 0 then
                if battery_state.plugged == 1 then
                    if battery_state.capacity_cal_count >= battery_state.capacity_cal_max then
                        battery_state.capacity = battery_state.capacity + 1
                        battery_state.capacity_init = 1
                        battery_state.capacity_cal_count = 0
                        battery_state.current_num = 0
                    else
                        battery_state.capacity_cal_count = battery_state.capacity_cal_count + 1
                        if battery_state.current_num_flag == 0 then
                            battery_state.current_num = 0
                        end
                        battery_state.current_num_flag = 1
                        battery_state.current_num = battery_state.current_num + current_now
                    end
                else
                    if battery_state.capacity_cal_count >= battery_state.capacity_cal_max then
                        battery_state.capacity = battery_state.capacity
                        if battery_state.current_num > 0 then
                            battery_state.capacity = battery_state.capacity + 1
                        end
                        battery_state.capacity_init = 1
                        battery_state.current_num = 0
                        battery_state.capacity_cal_count = 0
                    else
                        battery_state.capacity_cal_count = battery_state.capacity_cal_count + 1
                        if battery_state.current_num_flag == 1 then
                            battery_state.current_num = 0
                        end
                        battery_state.current_num_flag = 0
                        battery_state.current_num = battery_state.current_num + current_now
                    end
                end
            end
        else
            if math.abs(cap_offset) >= 2 then
                battery_state.capacity_fail_count = battery_state.capacity_fail_count + 1
                if battery_state.capacity_fail_count >= 6 then
                    battery_state.capacity = capacity
                    battery_state.capacity_init = 1
                    battery_state.capacity_fail_count = 0
                else
                    battery_state.capacity = battery_state.capacity
                    battery_state.capacity_init = 1
                end
            else
                if battery_state.capacity_init == 0 then
                    if capacity ~= 0 then
                        battery_state.capacity = capacity
                        battery_state.capacity_init = 1
                        battery_state.capacity_fail_count = 0
                    end

                    if battery_state.capacity_init_count >= 6 then
                        battery_state.capacity = capacity
                        battery_state.capacity_init = 1
                        battery_state.capacity_fail_count = 0
                    end

                    battery_state.capacity_init_count = battery_state.capacity_init_count +1
                end
            end
            battery_state.current_num = 0
            battery_state.capacity_cal_count = 0
        end

        battery_state.temp = tostring(temp / 10)
        battery_state.cycle_count = cycle_count
        local pwr_sink = read_aw35615_pwr()
        if battery_state.capacity <= 20 and not battery_state.low_battery_flag and battery_state.capacity_init == 1 then
            if pwr_sink == 0 then
                battery_state.low_battery_flag = true
                ubus.call("gl_screen", "set", {method = "low battery"})
            elseif pwr_sink == 1 and current_now < 0 then
                battery_state.low_battery_flag = true
                ubus.call("gl_screen", "set", {method = "low battery"})
            else
                if battery_state.critical_battery_flag then
                    battery_state.critical_battery_flag = false
                    ubus.call("gl_screen", "set", {method = "clear low battery"})
                end
            end
        end

        if battery_state.capacity > 20 and battery_state.low_battery_flag then
             ubus.call("gl_screen", "set", {method = "clear low battery"})
            battery_state.low_battery_flag = false
        end

        if battery_state.capacity <= 5 and not battery_state.critical_battery_flag  and battery_state.capacity_init == 1 then
            if pwr_sink == 0 then
                battery_state.critical_battery_flag = true
                ubus.call("gl_screen", "set", {method = "critical battery"})
            elseif pwr_sink == 1 and current_now < 0 then
                battery_state.critical_battery_flag = true
                ubus.call("gl_screen", "set", {method = "critical battery"})
            else
                if battery_state.critical_battery_flag then
                    battery_state.critical_battery_flag = false
                    ubus.call("gl_screen", "set", {method = "clear critical battery"})
                end
            end
        end

        if battery_state.capacity > 5 and battery_state.critical_battery_flag then
            battery_state.critical_battery_flag = false
            ubus.call("gl_screen", "set", {method = "clear critical battery"})
        end

        if (battery_state.capacity <= 0 and sgm41542s_ibat <= 0 and sgm41600_ibat <= 0 and capacity <=  0) or
            (sgm41542s_vbat < 3300 and sgm41600_vbat < 3300 and cw2217_voltage < 3300000) then
                if not battery_state.check_poweroff_flag and battery_state.capacity_init == 1 then
                    battery_state.check_poweroff_flag = true
                    battery_state.check_poweroff_num = 0
                    check_poweroff_timer:set(1)
                end
        else
            if battery_state.check_poweroff_flag then
                battery_state.check_poweroff_flag = false
                check_poweroff_timer:cancel()
            end
        end

        if vreg == 4180000 and cw2217_voltage >= 4000000 and current_now <= 0 and temp > 410 then
            if pwr_sink == 1 then
                battery_state.limit_voltage_check_count = battery_state.limit_voltage_check_count + 1
                if battery_state.limit_voltage_check_count >= 12 then
                    battery_state.limit_voltage_check_count = 0
                    if not battery_state.limit_voltage_flag then
                        battery_state.limit_voltage_flag = true
                        ubus.call("gl_screen", "set", {method = "charging paused"})
                    end
                end
            else
                battery_state.limit_voltage_check_count = 0
                if battery_state.limit_voltage_flag then
                    battery_state.limit_voltage_flag = false
                    ubus.call("gl_screen", "set", {method = "charging recovery"})
                end
            end
        else
            battery_state.limit_voltage_check_count = 0
            if battery_state.limit_voltage_flag then
                battery_state.limit_voltage_flag = false
                ubus.call("gl_screen", "set", {method = "charging recovery"})
            end
        end
    else
        if battery_state.check_poweroff_flag then
            battery_state.check_poweroff_flag = false
            check_poweroff_timer:cancel()
        end
    end

    if battery_state.capacity > 100 then
        battery_state.capacity = 100
        battery_state.capacity_init = 1
    end

    if battery_state.capacity < 0 then
        battery_state.capacity = 0
        battery_state.capacity_init = 1
    end

    if battery_state.capacity ~= mcu.capacity then
        ubus.call("gl_screen", "set", {method = "battery_status_change"})
    end

    mcu.capacity = battery_state.capacity
    mcu.temp = battery_state.temp
    mcu.charge_cycle = battery_state.cycle_count
    local temp_num = tonumber(mcu and mcu.temp) or 0
    local capacity_num = tonumber(mcu and mcu.capacity) or 0

    warning_temp_high(temp_num)
    warning_temp_low(temp_num)
    warning_capacity(capacity_num)
    battery_state.read_cw2217_flag = false
end

local function ubus_init()
    local ubus_conn = ubus.connect()

    ubus_conn:add('mcu',{
            status = {
                function(req)
                    mcu_status_flag = 1
                    local res = {
                        abnormal = mcu.abnormal == 1,
                        abnormal_type = mcu.abnormal_type,
                        temperature = mcu.temp,
                        charge_percent = mcu.capacity,
                        charging_status = mcu.chg_state,
                        charge_cnt = mcu.charge_cycle,
                        fastcharge = mcu.fastcharge
                    }

                    ubus_conn:reply(req, res)
                end
            },
            reload = {
                function(req)
                    if battery_info.serial then
                        flag = 2
                        timer_monitor()
                    end
                end
            },
            version = {
                function(req)
                    if battery_info.serial then
                        local have_data = get_serial_data(dev, baudrate, "{\"version\": \"1\"}")
                        if json_data_flag then
                            if have_data then
                                local ok, data = pcall(cjson.decode, have_data)
                                if ok then
                                    ubus_conn:reply(req, data)
                                end
                            end
                        else
                            if have_data and type(have_data) == "string" then
                                local res = {}
                                res.version = string.gsub(have_data,"\n","")
                                ubus_conn:reply(req, res)
                            end
                        end
                    else
                        local res = {}
                        res.version = "1.0"
                        ubus_conn:reply(req, res)
                    end
                end
            },
            cmd_json = {
                function(req, msg)
                    if battery_info.serial then
                        local cmd = cjson.encode(msg)
                        if cmd then
                            cmd = string.gsub(cmd, ":",": ")
                            local have_data = get_serial_data(dev, baudrate, cmd)
                            if have_data then
                                local ok, data = pcall(cjson.decode, have_data)
                                if ok then
                                ubus_conn:reply(req, data)
                            end
                            end
                        end
                    end
                end, { cmd = ubus.STRING }
            },
            cmd_string = {
                function(req, msg)
                    if battery_info.serial then
                        if msg.cmd then
                            local have_data = get_serial_data(dev, baudrate, msg.cmd)
                            if have_data then
                                local res = {
                                    result = have_data or "",
                                }
                                ubus_conn:reply(req, res)
                            end
                        end
                    end
                end, { cmd = ubus.STRING }
            },
            set_warning = {
                function(req, msg)
                    for name, table in pairs(msg) do
                        if warning[name] ~= nil then
                            for k, v in pairs(table) do
                                if warning[name][k] ~= nil then
                                    warning[name][k] = v
                                end
                            end
                            warning[name].warned = false
                        end
                    end
                end, {
                    temp_high = ubus.TABLE,
                    temp_low = ubus.TABLE,
                    capacity = ubus.TABLE
                }
            },
            get_warning = {
                function(req)
                    ubus_conn:reply(req, warning)
                end
            },
            system_reboot = {
                function(req, msg)
                    if battery_info.serial then
                        local res = {}
                        res.system = "reboot"
                        send_msg_lock_flag = 1
                        uart_write(res)
                    end
                end
            },
            system_update = {
                function(req, msg)
                    if battery_info.serial then
                        local res = {}
                        res.system = "updating"
                        send_msg_lock_flag = 1
                        uart_write(res)
                    end
                end
            },
            system_reft = {
                function(req, msg)
                    if battery_info.serial then
                        local res = {}
                        if type(msg) == "table" and msg.system and msg.system == "reft" then
                            res.system = "reft"
                            send_msg_lock_flag = 1
                            uart_write(res)
                        end
                    end
                end
            },
            system_renw = {
                function(req, msg)
                    if battery_info.serial then
                        local res = {}
                        if type(msg) == "table" and msg.system and msg.system == "renw" then
                            res.system = "renw"
                            uart_write(res)
                        end
                    end
                end
            },
            reset_button = {
                function(req, msg)
                    if battery_info.serial then
                        local res = {}
                        if type(msg) == "table" and msg.button and send_msg_lock_flag ~= 1 then
                            res.button = msg.button
                            uart_write(res)
                        end
                    end
                end
            },
            set_product_flag = {
                function(req, msg)
                    local res = {}
                    if type(msg) == "table" and msg.product_flag and msg.product_flag == "1" then
                        product_test_flag = 1
                        res.data = "product_test_up"
                        ubus_conn:reply(req, res)
                    end

                    if type(msg) == "table" and msg.product_flag and msg.product_flag == "0" then
                        product_test_flag = 0
                        res.data = "product_test_off"
                        ubus_conn:reply(req, res)
                    end
                end
            },
            send_custom_msg = {
                function(req, msg)
                    if battery_info.serial then
                        local data = format_massage(msg)
                        local res = {}
                        if read_lock_flag == 0 then
                            res.data = get_serial_data(dev, baudrate, data)
                        else
                            res.data = "read fail,file lock"
                        end
                        ubus_conn:reply(req, res)
                    end
                end
            },
            update_battery_state = {
                function(req, msg)
                    local res = {}
                    if battery_info.driver == "cw2217" then
                        update_battery_timer:set(0.1)
                    end

                    res = {
                        result = "ok"
                    }

                    ubus_conn:reply(req, res)
                end
            }
    })

    while true do
        time.sleep(30)
    end
end

local function read_mcu_state()
    local have_data = get_serial_data(dev, baudrate, "{\"mcu_status\": \"1\"}")
    if have_data then
        local ok, data = pcall(cjson.decode, have_data)
        if ok then
            mcu.temp = data.temp or mcu.temp
            mcu.capacity = data.capacity or mcu.capacity
            mcu.chg_state = data.chg_state or mcu.chg_state
            mcu.charge_cycle = data.charge_cycle or mcu.charge_cycle
            mcu.abnormal = data.code or mcu.abnormal
        else
            local capacity, temp, chg_state, charge_cycle = have_data:match('{OK},(%d+),(-?%d+%.%d+),(%d+),(%d+)')

        if not temp then
            capacity, temp, chg_state, charge_cycle = have_data:match('{OK},(%d+),(-?%d+),(%d+),(%d+)')
            temp = temp and tostring(temp / 10)
        end

            mcu.temp = temp or mcu.temp
            mcu.capacity = capacity or mcu.capacity
            mcu.chg_state = chg_state or mcu.chg_state
            mcu.charge_cycle = charge_cycle or mcu.charge_cycle
        end
    end

    local temp_num = tonumber(mcu and mcu.temp) or 0
    local capacity_num = tonumber(mcu and mcu.capacity) or 0

    warning_temp_high(temp_num)
    warning_temp_low(temp_num)
    warning_capacity(capacity_num)
end

local function mcu_init()
    local c = uci.cursor()
    oled_flag = c:get("glconfig", "mcu", "oled")
end

local function warning_init()
    local c = uci.cursor()
    warning.temp_high.enable = c:get("mcu", "temp_high", "enable") == "1"
    warning.temp_high.interval = tonumber(c:get("mcu", "temp_high", "interval") or warning.temp_high.interval)
    warning.temp_high.value = tonumber(c:get("mcu", "temp_high", "value") or warning.temp_high.value)

    warning.temp_low.enable = c:get("mcu", "temp_low", "enable") == "1"
    warning.temp_low.interval = tonumber(c:get("mcu", "temp_low", "interval") or warning.temp_low.interval)
    warning.temp_low.value = tonumber(c:get("mcu", "temp_low", "value") or warning.temp_low.value)

    warning.capacity.enable = c:get("mcu", "capacity", "enable") == "1"
    warning.capacity.interval = tonumber(c:get("mcu", "capacity", "interval") or warning.capacity.interval)
    warning.capacity.value = tonumber(c:get("mcu", "capacity", "value") or warning.capacity.value)
end

local function get_mcu_serial_info()
    local mcu_info = file.readfile('/proc/gl-hw-info/mcu') or ''

    if #mcu_info > 0 then
        local dev, baudrate = mcu_info:match("([^,]+),([^,]+)")
        return dev, baudrate
    end

    return nil, nil
end

local function serial_init()
    local c = uci.cursor()

    local device, rate = get_mcu_serial_info()
    dev = c:get("glconfig", "mcu", "dev") or device or "/dev/ttyUSB0"
    baudrate = tonumber(c:get("glconfig", "mcu", "baudrate") or rate or "9600")
    flock(lockfile)
    log.info("open %s for baudrate %d", dev, baudrate)
end

local function battery_init()
    battery_info.driver = file.readfile('/proc/gl-hw-info/mcu', '*l') or ''
    if battery_info.driver == "cw2217" then
        battery_info.serial = false
    else
        battery_info.serial = true
    end
end

battery_init()
warning_init()

if battery_info.serial then
    serial_init()
end

mcu_init()


sys.signal(sys.SIGINT, function()
    log.info('\nGot SIGINT, now quit')
    unflock(lockfile)
    eco.unloop()
end)

sys.signal(sys.SIGTERM, function()
    log.info('\nGot SIGTERM, now quit')
    unflock(lockfile)
    eco.unloop()
end)

sys.signal(sys.SIGALRM, function()
    log.info('\nGot SIGALRM, now quit')
    unflock(lockfile)
    eco.unloop()
end)

sys.signal(sys.SIGSEGV, function()
    log.info('\nGot SIGALRM, now quit')
    unflock(lockfile)
    eco.unloop()
end)

eco.run(ubus_init)
clear_power_saving()

log.set_level(log.INFO)

if oled_flag then
    eco.run( function()
        local send_msg_time = 0
        while true do
            if product_test_flag ~= 1 then
                timer_monitor()
            end
        -- 30s超时释放锁
            if send_msg_lock_flag == 1 then
                send_msg_unlock_count = send_msg_unlock_count + 1
                if send_msg_unlock_count > 6 then
                   send_msg_lock_flag = 0
                   send_msg_unlock_count = 0
                end
            else
                send_msg_unlock_count = 0
            end
            if mcu_status_flag == 0 then
                send_msg_time = send_msg_time + 1
                if send_msg_time > 12 then
                    mcu_status_flag = 1
                    send_msg_time = 0
                end
            else
                send_msg_time = 0
            end

            collectgarbage("collect")

            time.sleep(5)
        end
    end)
else
    json_data_flag = 1
    eco.run( function()
        while true do
            if battery_info.driver == "cw2217" then
                read_cw2217_state()
                update_battery_timer:set(0.1)
                time.sleep(5)
            else
                read_mcu_state()
                time.sleep(30)
            end
        end
    end)
end
