import re

from ctf.utils.log import debug_log

_FLAG_PATTERN = re.compile(
    r"(?:(?:(?<=^)|(?<![0-9a-zA-Z_-]))[0-9a-zA-Z_-]+(?:ctf|flag|fl4g)|ctf|flag|fl4g)[ \-:]?\{[^{}]+}",
    re.IGNORECASE
)

def match_flag(text: str) -> str | None:
    debug_log(f"输入文本: {text[:256] if text else 'None'}...")
    if not text:
        debug_log("文本为空，返回 None")
        return None
    match = _FLAG_PATTERN.search(text)
    result = match.group(0) if match else None
    return result

def match_flags(text: str) -> list[str]:
    debug_log(f"输入文本: {text[:256] if text else 'None'}...")
    if not text:
        debug_log("文本为空，返回空列表")
        return []
    results = _FLAG_PATTERN.findall(text)
    debug_log(f"匹配到 {len(results)} 个 flag")
    return results