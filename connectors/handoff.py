"""Supply only the explicitly declared local text materials and retain the reply."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".rst", ".tex", ".py",
                 ".yaml", ".yml", ".toml", ".log"}
MAX_FILE_BYTES = 24 * 1024
MAX_TOTAL_BYTES = 64 * 1024


def collect_materials(handoff, cwd):
    originals = []
    remaining = MAX_TOTAL_BYTES
    for item in handoff.get("materials", []):
        ref = item["ref"]
        entry = {"ref": ref}
        originals.append(entry)
        if re.match(r"^[a-z][a-z0-9+.-]*://", ref, re.IGNORECASE):
            entry["unavailable"] = "本机执行器未抓取外部链接"
            continue
        try:
            path = (cwd / ref).resolve()
        except (OSError, RuntimeError, ValueError):
            entry["unavailable"] = "文件位置无法解析"
            continue
        if not path.is_relative_to(cwd) or path.suffix.lower() not in TEXT_SUFFIXES:
            entry["unavailable"] = "需要位于指定目录内的获准 UTF-8 文本原件"
            continue
        if not path.is_file():
            entry["unavailable"] = "文件未找到"
            continue
        limit = min(MAX_FILE_BYTES, remaining)
        try:
            with path.open("rb") as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                entry["unavailable"] = "超出随交接传送的大小，请单独选择带定位的段落"
                continue
            text = content.decode("utf-8-sig")
        except (OSError, UnicodeError):
            entry["unavailable"] = "无法读取为 UTF-8 文本"
            continue
        if "\x00" in text:
            entry["unavailable"] = "文件含二进制数据"
            continue
        remaining -= len(content)
        entry.update(text=text, sha256=hashlib.sha256(content).hexdigest())
    return originals


def write_text(path, data):
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + "-", suffix=".partial", mode="w",
        encoding="utf-8", newline="\n", delete=False,
    ) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def decode_result(result):
    text = result.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        response = json.loads(text)
    except ValueError:
        return {}
    return response if isinstance(response, dict) else {}


def persist_report(directory, run, turn, response):
    content = response.get("deliverable_markdown")
    if not isinstance(content, str) or not content.strip():
        content = turn.result
    report = directory / "交付.md"
    # Use a fence longer than any run of backticks in the response.
    length = max((len(match.group()) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(length + 1, 3)
    text = (
        f'# 本轮交付\n\n派工编号 `{run["dispatch"]}`\n\n'
        f'客户端 `{run["client"]}` · 会话 `{turn.session}`\n\n'
        f'{fence}text\n{content.strip()}\n{fence}\n'
    )
    write_text(report, text)
    return str(report)
