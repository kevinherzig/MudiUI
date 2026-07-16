local M = {}

local STYPE = {
    NULL = 0,
    INTEGER = 1,
    STRING = 2,
    FLOAT = 3,
    POINTER = 4,
    REFERENCE = 5
}

local function is_number(str)
    return tonumber(str) ~= nil
end

local function isFloatString(s)
    return tostring(s):find("[.]") ~= nil
end

M.parse_config = function(value)
    local config = {}
    local config_type
    local match_key
    local match_value
    local match_type
    if (value == nil) then
        return config
    end
    for line in value:gmatch("[^\n]+") do
        match_key, match_value = line:match("^(%S+)%s+(.+)")
        if match_key ~= nil and match_value ~= nil then
            match_value = match_value:gsub("%s+$", "")
            if match_value ~= "" then
                if (match_value == "NULL") then
                    config_type = STYPE.NULL
                else
                    if (is_number(match_value)) then
                        if (isFloatString(match_value)) then
                            config_type = STYPE.FLOAT
                        else
                            config_type = STYPE.INTEGER
                        end
                    else
                        match_type = match_value:match("^\"(.*)\"$")
                        if match_type ~= nil then
                            match_value = match_type
                            config_type = STYPE.STRING
                        else
                            match_type = match_value:match("^&(.+)$")
                            if (match_type ~= nil) then
                                match_value = match_type
                                config_type = STYPE.POINTER
                            else
                                config_type = STYPE.REFERENCE
                            end
                        end
                    end
                end
                config[match_key] = {}
                config[match_key]["type"] = config_type
                if config_type == STYPE.INTEGER or config_type == STYPE.FLOAT then
                    config[match_key]["value"] = tonumber(match_value)
                else
                    config[match_key]["value"] = match_value
                end
            end
        end
    end
    return config
end

M.parse_file = function(file)
    local handle = io.open(file, "r")
    local output = handle:read("*a")
    local parse_value = M.parse_config(output)
    handle:close(handle)
    return parse_value
end

local function config_reference_rec(parse_value, node)
    if node ~= nil then
        if node.type == STYPE.REFERENCE then
            node = config_reference_rec(parse_value, parse_value[node.value])
        end
    end
    return node
end

M.get_value = function(parse_value, key, config_type)
    local res
    local node = parse_value[key]
    node = config_reference_rec(parse_value, node)
    if node ~= nil then
        if node.type ~= config_type then
            node = nil
        end
    end
    if node == nil then
        if config_type == STYPE.INTEGER or config_type == STYPE.FLOAT then
            res = 0
        end
    else
        res = node.value
    end
    return res
end

M.check_value = function(parse_value, key, config_type)
    local res = false
    local node = parse_value[key]
    node = config_reference_rec(parse_value, node)
    if node ~= nil and node.type == config_type then
        res = true
    end
    return res
end

M.foreach = function(parse_value, func, args)
    local res = true
    for key, value in pairs(parse_value) do
        if type(func) == "function" then
            res = func(key, value.type, value.value, args)
            if res ~= true then
                return res
            end
        end
    end
    return res
end

M.STYPE = STYPE

return M

