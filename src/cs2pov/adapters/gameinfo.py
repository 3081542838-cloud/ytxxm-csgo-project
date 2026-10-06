"""Create a reversible SearchPaths insertion, preserving every original byte."""
import re


class GameInfoError(ValueError):
    pass


def add_pov_search_path(original: bytes) -> bytes:
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError as error:
        raise GameInfoError("gameinfo.gi 不是有效 UTF-8 文件。") from error
    pattern = re.compile(r'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    tokens = [(m.group().strip('"'), m.start(), m.end()) for m in pattern.finditer(text)
              if not m.group().startswith("//")]
    scopes = [i for i, token in enumerate(tokens) if token[0].casefold() == "searchpaths"]
    if len(scopes) != 1:
        raise GameInfoError("必须存在唯一的 SearchPaths 区块。")
    start = scopes[0] + 1
    if start >= len(tokens) or tokens[start][0] != "{":
        raise GameInfoError("SearchPaths 格式无法确认。")
    depth, candidates, closed = 1, [], False
    i = start + 1
    while i < len(tokens):
        token = tokens[i][0]
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                closed = True
                break
        elif depth == 1 and token.casefold() == "game" and i + 1 < len(tokens):
            value = tokens[i + 1][0].casefold().replace("\\", "/")
            if value.endswith("/pov.vpk") or value == "pov.vpk":
                raise GameInfoError("已有 POV 搜索路径，不能重复部署。")
            if value == "csgo":
                candidates.append(i)
        i += 1
    if not closed or len(candidates) != 1:
        raise GameInfoError("无法找到唯一的 Game csgo 入口。")
    offset = tokens[candidates[0]][1]
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end < 0:
        raise GameInfoError("入口没有独立行。")
    line = text[line_start:line_end + 1]
    match = re.fullmatch(r'([ \t]*)"?Game"?[ \t]+"?csgo"?[ \t]*(?://[^\r\n]*)?\r?\n', line, re.IGNORECASE)
    if not match:
        raise GameInfoError("Game csgo 入口不是支持的独立行格式。")
    newline = "\r\n" if line.endswith("\r\n") else "\n"
    inserted = f"{match[1]}Game\tcsgo/pov.vpk{newline}"
    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8"
    return (text[:line_start] + inserted + text[line_start:]).encode(encoding)
