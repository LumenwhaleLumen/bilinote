#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bilinote —— 把 B站视频转成一份能用的文字笔记。

用法：
    python bilinote.py BV1xxxxxxxxx                 # 打印到标准输出
    python bilinote.py https://www.bilibili.com/video/BV1xx -o note.md
    python bilinote.py BV1xx --model medium         # 换更大的 Whisper 模型

两条路：
    1) 有字幕轨 → 直接取官方/用户上传的字幕（最快，不需要跑模型）
    2) 没字幕轨 → 用 faster-whisper 本地转写音频（免费，慢，靠 CPU）

只依赖：requests（有）、faster-whisper + yt-dlp（转写时才需要）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HEAD = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
HOME = os.path.dirname(os.path.abspath(__file__))


def bv_of(target: str) -> str:
    m = re.search(r"BV[0-9A-Za-z]{10}", target)
    if m:
        return m.group(0)
    raise SystemExit(f"看不懂这个输入，需要 BV 号或视频链接：{target}")


def load_cookies() -> dict:
    """能读到登录凭证就用（字幕轨多半要登录才给），读不到就匿名跑。"""
    for p in (
        os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "bili_credential.json"),
    ):
        try:
            with open(p, encoding="utf-8") as f:
                c = json.load(f)
            return {
                "SESSDATA": requests.utils.unquote(c.get("sessdata", "")),
                "bili_jct": c.get("bili_jct", ""),
                "buvid3": c.get("buvid3", ""),
                "DedeUserID": c.get("dedeuserid", ""),
            }
        except Exception:
            continue
    return {}


def video_info(bv: str, cookies: dict) -> dict:
    r = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bv}, headers=HEAD, cookies=cookies, timeout=20,
    )
    j = r.json()
    if j.get("code") != 0:
        raise SystemExit(f"取视频信息失败：{j.get('code')} {j.get('message')}")
    d = j["data"]
    return {
        "bvid": d["bvid"],
        "aid": d["aid"],
        "cid": d["cid"],
        "title": d["title"],
        "author": d["owner"]["name"],
        "duration": d["duration"],
        "desc": (d.get("desc") or "").strip(),
    }


def fetch_subtitle(info: dict, cookies: dict) -> list[tuple[float, str]]:
    """返回 [(秒, 文本)]；没有字幕轨就返回空列表。"""
    r = requests.get(
        "https://api.bilibili.com/x/player/v2",
        params={"bvid": info["bvid"], "cid": info["cid"]},
        headers=HEAD, cookies=cookies, timeout=20,
    )
    try:
        sub = r.json()["data"]["subtitle"]
    except Exception:
        return []
    urls = [s.get("subtitle_url") or "" for s in (sub.get("subtitles") or [])]
    for u in urls:
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        try:
            body = requests.get(u, headers=HEAD, timeout=20).json()
        except Exception:
            continue
        out = []
        for seg in body.get("body") or []:
            txt = (seg.get("content") or "").strip()
            # B站的「♪音乐♪」占位行不算内容，留着会把整份笔记变成音乐符号
            if not txt or txt.startswith("♪") or txt.startswith("♫"):
                continue
            out.append((float(seg.get("from", 0)), txt))
        if out:
            # 护栏：字幕时间戳必须落在视频时长内，否则多半拿到了别人的字幕
            limit = info.get("duration", 0) + 5
            inside = [o for o in out if o[0] <= limit]
            if len(inside) >= max(3, len(out) * 0.8):
                return inside
            return []
    return []


def transcribe(info: dict, model_size: str) -> list[tuple[float, str]]:
    """本地转写：yt-dlp 拉音频，faster-whisper 出时间戳。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel
    from yt_dlp import YoutubeDL

    tmp = os.environ.get("BILINOTE_TMP") or os.path.join(HOME, "work")
    os.makedirs(tmp, exist_ok=True)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, f"{info['bvid']}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": HEAD,
        "paths": {"home": tmp, "temp": tmp},
    }
    with YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(f"https://www.bilibili.com/video/{info['bvid']}", download=True)
        audio = ydl.prepare_filename(meta)

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(audio, language="zh", vad_filter=True, beam_size=5)
    out = [(float(s.start), s.text.strip()) for s in segs]
    try:
        os.remove(audio)
    except OSError:
        pass
    return out


def mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


CJK = re.compile(r"[\u4e00-\u9fff]")


def tidy(text: str) -> str:
    """转写稿没有标点，靠简单规则补一点，读起来不那么喘。"""
    t = re.sub(r"\s+", "", text)
    if not t:
        return ""
    if t[-1] not in "。！？，、；：":
        t += "。"
    return t


def to_sentences(segments: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """把逐段转写按「时间空档」切成句子：停顿超过 0.55 秒就算一句结束。"""
    out: list[tuple[float, str]] = []
    buf, start, prev_end = [], 0.0, None
    for t, text in segments:
        text = tidy(text)
        if not text:
            continue
        if not buf:
            start = t
        buf.append(text)
        gap_next = None
        if prev_end is not None:
            gap_next = t - prev_end
        prev_end = t
        if gap_next is not None and gap_next > 0.55:
            out.append((start, "".join(buf)))
            buf = []
    if buf:
        out.append((start, "".join(buf)))
    return out


def summarize(sentences: list[tuple[float, str]], top_n: int = 5) -> list[tuple[float, str]]:
    """抽取式摘要：不调模型，按字频给句子打分，挑分高的几句按原顺序返回。"""
    if len(sentences) <= top_n:
        return sentences
    freq: dict[str, int] = {}
    for _, s in sentences:
        for i in range(len(s) - 1):
            bg = s[i:i + 2]
            if CJK.match(bg[0]) and bg not in "。，、；：！？":
                freq[bg] = freq.get(bg, 0) + 1
    scored = []
    for idx, (t, s) in enumerate(sentences):
        score = 0
        for i in range(len(s) - 1):
            bg = s[i:i + 2]
            score += freq.get(bg, 0)
        score = score / (len(s) ** 0.5 + 1)
        scored.append((score, idx, t, s))
    picked = sorted(sorted(scored, key=lambda x: -x[0])[:top_n], key=lambda x: x[1])
    return [(t, s) for _, _, t, s in picked]


def build_note(info: dict, segments: list[tuple[float, str]], source: str) -> str:
    sentences = to_sentences(segments)
    lines = []
    lines.append(f"# {info['title']}")
    lines.append("")
    lines.append(f"- UP：{info['author']}")
    lines.append(f"- 时长：{mmss(info['duration'])}")
    lines.append(f"- 链接：https://www.bilibili.com/video/{info['bvid']}")
    lines.append(f"- 文本来源：{source}")
    lines.append("")
    if info.get("desc"):
        lines.append("> " + info["desc"].replace("\n", "\n> "))
        lines.append("")
    if sentences:
        lines.append("## 要点（自动抽取，可能有偏差）")
        lines.append("")
        for t, s in summarize(sentences):
            lines.append(f"- [{mmss(t)}] {s}")
        lines.append("")
    lines.append("## 正文（带时间戳）")
    lines.append("")
    # 按句子拼段：攒够约 150 字或遇到较长停顿就另起一段
    buf, start, prev_t = [], 0.0, None
    for t, s in sentences:
        if not buf:
            start = t
        buf.append(s)
        long_pause = prev_t is not None and t - prev_t > 3.0
        prev_t = t
        if sum(len(x) for x in buf) >= 150 or long_pause:
            lines.append(f"**[{mmss(start)}]** " + "".join(buf))
            lines.append("")
            buf = []
    if buf:
        lines.append(f"**[{mmss(start)}]** " + "".join(buf))
        lines.append("")
    total = sum(len(t) for _, t in segments)
    lines.append(f"---\n\n共 {len(sentences)} 句 / 约 {total} 字。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="B站视频 → 文字笔记")
    ap.add_argument("target", help="BV 号或视频链接")
    ap.add_argument("-o", "--out", help="写到这个文件（不给就打到标准输出）")
    ap.add_argument("--model", default="small", help="Whisper 模型：tiny/base/small/medium")
    ap.add_argument("--no-asr", action="store_true", help="没字幕就报错，不做本地转写")
    args = ap.parse_args()

    bv = bv_of(args.target)
    cookies = load_cookies()
    info = video_info(bv, cookies)
    print(f"[*] {info['title']}（{mmss(info['duration'])}）", file=sys.stderr)

    t0 = time.time()
    segs = fetch_subtitle(info, cookies)
    source = "B站字幕轨"
    if not segs:
        if args.no_asr:
            raise SystemExit("这条没有字幕轨；加 --no-asr 之外的方式或去掉该参数可本地转写。")
        print("[*] 没有字幕轨，开始本地转写（要几分钟）…", file=sys.stderr)
        segs = transcribe(info, args.model)
        source = f"本地转写（whisper-{args.model}）"
    if not segs:
        raise SystemExit("没拿到任何文本。")
    print(f"[*] 用时 {time.time() - t0:.1f}s，共 {len(segs)} 段", file=sys.stderr)

    note = build_note(info, segs, source)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(note)
        print(f"[*] 已写入 {args.out}", file=sys.stderr)
    else:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
