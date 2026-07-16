local script_path = arg[0]
local script_dir = script_path:match("(.*[/\\])") or "."
package.path = package.path .. ";" .. script_dir .. "/?.lua"

local config = require "kv_config"
local lfs = require "lfs"

local config_base_dir = "./config"
local svg_source_dir = "./svg"
local screen_reference_file = nil
local screen_reference_config = nil
local gen_image_dirs = {}

local function read_config_file(config_path)
    if lfs.attributes(config_path, "mode") then
        return config.parse_file(config_path)
    end
    return {}
end

local function file_exist(path)
    local f = io.open(path, "r")
    if f then
        f:close()
        return true
    end
    return false
end

local function dir_exist(path)
    local attr = lfs.attributes(path)
    return attr and attr.mode == "directory"
end

local function traverse_dir(path, func, args)
    local res = true
    if type(func) ~= "function" then
        return false
    end
    for file in lfs.dir(path) do
        if file ~= "." and file ~= ".." then
            local full_path = path .. "/" .. file
            local attr = lfs.attributes(full_path)
            res = func(path, file, full_path, attr, args)
            if res ~= true then
                return res
            end
        end
    end
    return res
end

local function string_loop_split(key, reverse, func, args)
    local res = true;
    local parts = {}
    if key == "" then
        return res;
    else
        if type(func) == "function" then
            res = func(key, "", args)
            if res ~= true then
                return res
            end
        end
    end
    for part in key:gmatch("[^_]+") do
        table.insert(parts, part)
    end
    if #parts == 1 then
        return res
    end
    local result = {}
    for i = 1, #parts - 1 do
        local next_res = {}
        local left = table.concat(parts, "_", 1, i)
        local right = table.concat(parts, "_", i + 1, #parts)
        next_res.left = left
        next_res.right = right
        table.insert(result, next_res)
    end
    if reverse then
        for i = #result, 1, -1 do
            local rvalue = result[i];
            if type(func) == "function" then
                res = func(rvalue.left, rvalue.right, args)
                if res ~= true then
                    return res
                end
            end
        end
    else
        for i, _ in ipairs(result) do
            local rvalue = result[i];
            if type(func) == "function" then
                res = func(rvalue.left, rvalue.right, args)
                if res ~= true then
                    return res
                end
            end
        end
    end
    return res;
end

local function search_config_dirs(path, file, full_path, attr, args)
    if attr.mode == "directory" then
        traverse_dir(full_path, search_config_dirs, args)
    else
        if file == "gen_image" then
            local project_dir = full_path:match("config/([^/]+)/gen_image")
            if project_dir then
                gen_image_dirs[#gen_image_dirs + 1] = {
                    path = full_path:match("(.*)/gen_image"),
                    project_name = project_dir,
                    config_file = full_path,
                    type = "project"
                }
            end
        elseif file == "screen_reference" then
            if screen_reference_file == nil then
                screen_reference_file = full_path
                screen_reference_config = read_config_file(full_path)
                local screen_dir = full_path:match("(.*)/screen_reference")
                if screen_dir then
                    gen_image_dirs[#gen_image_dirs + 1] = {
                        path = screen_dir,
                        project_name = screen_dir:match("config/([^/]+)$") or "screen_reference",
                        config_file = full_path,
                        type = "screen"
                    }
                end
            end
        end
    end
    return true
end

local function get_scale_factor(parse_value)
    local scale = config.get_value(parse_value, "scale", config.STYPE.FLOAT)
    if scale == 0 then
        scale = 1
    end
    return scale
end

local function convert_single_svg(svg_path, image_name, output_dir, width, height)
    local output_file = output_dir .. "/" .. image_name
    output_file = output_file .. ".png"
    if file_exist(output_file) then
        os.remove(output_file)
    end
    local cmd = string.format(
        'inkscape "%s" --export-filename="%s" --export-width=%d --export-height=%d --export-type="png" 2>/dev/null &',
        svg_path, output_file, width, height)
    print("  " .. image_name .. ".png" .. " (" .. width .. "x" .. height .. ")")
    os.execute(cmd)
    os.execute("sleep 0.1")
    return true
end

local function prepare_output_dir(output_dir)
    os.execute("mkdir -p " .. output_dir)
end

local function clean_output_dir(output_dir)
    os.execute("rm -rf " .. output_dir)
end

local function output_image_name_get(name)
    return name:gsub("_width$", ""):gsub("_height$", "")
end

local function config_gengerate_picture(parse_config, output_dir, scale, extra_svg, extra_svg_only)
    local config_handle = {}
    local processed_count = 0
    config.foreach(parse_config, function(key, type, value, args)
        if (type == config.STYPE.INTEGER) then
            local svg_path = ""
            local output_name = output_image_name_get(key)
            if config_handle[output_name] == true then
                return true
            end
            string_loop_split(output_name, true, function(left, right)
                local test_path
                if extra_svg ~= nil and dir_exist(extra_svg) then
                    test_path = extra_svg .. "/" .. left .. ".svg"
                    if file_exist(test_path) then
                        svg_path = test_path
                        return false
                    end
                end
                if not extra_svg_only then
                    test_path = svg_source_dir .. "/" .. left .. ".svg"
                    if file_exist(test_path) then
                        svg_path = test_path
                        return false
                    end
                end
                return true
            end, nil)
            if svg_path == "" then
                -- 未发现svg文件，处理下一个
                return true
            end
            local width = config.get_value(parse_config, output_name .. "_width", config.STYPE.INTEGER)
            local height = config.get_value(parse_config, output_name .. "_height", config.STYPE.INTEGER)
            if (scale ~= 1) then
                width = math.floor(width * scale + 0.5)
                height = math.floor(height * scale + 0.5)
            end
            if width == nil or height == nil then
                -- 未发现宽高，处理下一个
                return true
            end
            if width == 0 or height == 0 then
                -- 宽高不合理，处理下一个
                return true
            end
            convert_single_svg(svg_path, output_name, output_dir, width, height)
            config_handle[output_name] = true
            processed_count = processed_count + 1
        end
        return true
    end, nil)
    return processed_count
end

local function process_screen_reference(dir_info)
    local config_dir = dir_info.path
    local project_name = dir_info.project_name
    print("\n处理屏幕基准: " .. project_name)
    local output_dir = config_dir .. "/image"
    clean_output_dir(output_dir)
    prepare_output_dir(output_dir)
    local processed_count = config_gengerate_picture(screen_reference_config, output_dir, 1, nil, false)
    if processed_count <= 0 then
        clean_output_dir(output_dir)
    end
    print("  完成! 生成 " .. processed_count .. " 个图片")
end

local function process_project(dir_info)
    local config_dir = dir_info.path
    local project_name = dir_info.project_name
    print("\n处理项目: " .. project_name)
    local project_config = read_config_file(dir_info.config_file)
    local scale_factor = get_scale_factor(project_config)
    print("  缩放比例: " .. scale_factor)
    local output_dir = config_dir .. "/image"
    local extra_svg = config_dir .. "/svg"
    local processed_count = 0
    clean_output_dir(output_dir)
    prepare_output_dir(output_dir)
    if scale_factor ~= 1 and screen_reference_config then
        -- 如果有缩放比例则生成全部
        processed_count = processed_count +
                              config_gengerate_picture(screen_reference_config, output_dir, scale_factor, extra_svg,
                false)
    else
        -- 如果无缩放比例则生成额外svg的部分
        processed_count = processed_count +
                              config_gengerate_picture(screen_reference_config, output_dir, scale_factor, extra_svg,
                true)
    end
    processed_count = processed_count + config_gengerate_picture(project_config, output_dir, 1, extra_svg, false)
    if processed_count <= 0 then
        clean_output_dir(output_dir)
    end
    print("  完成! 生成 " .. processed_count .. " 个图片")
end

local function main()
    print("开始扫描配置目录...")
    traverse_dir(config_base_dir, search_config_dirs, nil)
    if not screen_reference_config and #gen_image_dirs == 0 then
        print("错误: 未找到screen_reference基准配置和gen_image配置")
        return
    end
    print("找到 " .. #gen_image_dirs .. " 个配置目录")
    for _, dir_info in ipairs(gen_image_dirs) do
        if dir_info.type == "screen" then
            process_screen_reference(dir_info)
        else
            process_project(dir_info)
        end
    end
    print("\n所有处理完成!")
end

main()
