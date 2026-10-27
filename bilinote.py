#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bilinote —— 把 B站视频转成一份能用的文字笔记。

用法：
    python bilinote.py BV1xxxxxxxxx
    python bilinote.py https://www.bilibili.com/video/BV1xx -o note.md
    python bilinote.py https://b23.tv/xxxxx  BV1yy  -o notes/   # 批量，写进目录
    python bilinote.py BV1xx --model medium      # 换更大的 Whisper 模型

两条路：
    1) 有字幕轨 → 直接取官方字幕（最快，不跑模型）
    2) 没字幕轨 → faster-whisper 本地转写（免费，慢，靠 CPU）

依赖只有 requests；本地转写另外要 faster-whisper。
（不再依赖 yt-dlp：音频直接用 B站 playurl 接口取。）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEAD = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
HOME = os.path.dirname(os.path.abspath(__file__))

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")


class NoteError(Exception):
    """给人看的错误：不打印堆栈，只说不成的原因。"""


# ── 输入解析 ────────────────────────────────────────────────────────────
def resolve_target(target: str) -> str:
    """把 BV 号 / 完整链接 / b23.tv 短链，统一解析成 BV 号。"""
    target = target.strip()
    m = BV_RE.search(target)
    if m:
        return m.group(0)

    if SHORT_RE.match(target):
        try:
            r = requests.get(target, headers=HEAD, timeout=15, allow_redirects=True)
            m = BV_RE.search(r.url or "")
            if m:
                return m.group(0)
        except requests.RequestException as e:
            raise NoteError(f"短链解析失败（{target}）：{e}") from e
        raise NoteError(f"这条短链没跳到视频页，可能失效了：{target}")

    if target.startswith("http"):
        raise NoteError(f"链接里找不到 BV 号：{target}")

    raise NoteError(f"看不懂这个输入，需要 BV 号或视频链接：{target}")


# ── B站接口 ─────────────────────────────────────────────────────────────
def _get(url: str, **kw) -> requests.Response:
    """带重试的 GET：限流/网络抖动重试两次，最后抛人话错误。"""
    last = None
    for i in range(3):
        try:
            r = requests.get(url, headers=HEAD, timeout=20, **kw)
            if r.status_code in (403, 412):
                raise NoteError(
                    "B站拒绝了请求（403/412，常见于风控或未登录）。"
                    "稍等几分钟再试，或把 B站凭证放进 bili_credential.json。")
            if r.status_code >= 500:
                last = NoteError(f"B站服务器出错（{r.status_code}），稍后重试。")
            else:
                return r
        except requests.Timeout:
            last = NoteError("请求超时，网络可能不稳。")
        except requests.RequestException as e:
            last = NoteError(f"网络错误：{e}")
        time.sleep(1.5 * (i + 1))
    raise last or NoteError("请求失败。")


def load_cookies() -> dict:
    """能读到登录凭证就用（字幕轨多半要登录才给），读不到就匿名跑。"""
    path = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "bili_credential.json")
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return {
            "SESSDATA": requests.utils.unquote(c.get("sessdata", "")),
            "bili_jct": c.get("bili_jct", ""),
            "buvid3": c.get("buvid3", ""),
            "DedeUserID": c.get("dedeuserid", ""),
        }
    except Exception:
        return {}


def video_info(bv: str, cookies: dict) -> dict:
    j = _get("https://api.bilibili.com/x/web-interface/view",
             params={"bvid": bv}, cookies=cookies).json()
    if j.get("code") != 0:
        raise NoteError(f"取视频信息失败：{j.get('code')} {j.get('message')}")
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
    """返回 [(秒, 文本)]；没有可信字幕轨就返回空列表。"""
    try:
        j = _get("https://api.bilibili.com/x/player/v2",
                 params={"bvid": info["bvid"], "cid": info["cid"]},
                 cookies=cookies).json()
        subs = (j.get("data") or {}).get("subtitle") or {}
    except (NoteError, ValueError):
        return []

    for s in subs.get("subtitles") or []:
        u = s.get("subtitle_url") or ""
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        try:
            body = _get(u).json()
        except (NoteError, ValueError):
            continue

        out = []
        for seg in body.get("body") or []:
            txt = (seg.get("content") or "").strip()
            # 「♪音乐♪」这类占位行不算内容
            if not txt or txt.startswith("♪") or txt.startswith("♫"):
                continue
            out.append((float(seg.get("from", 0)), txt))
        if not out:
            continue

        # 护栏一：字幕不该明显超出片长。
        # 实际情况里常见「AI 字幕轨比视频长」（正片被剪过），这时把越界部分裁掉即可，
        # 只要剩下的足够长、且基本覆盖到片尾，就认。
        dur = info.get("duration", 0) or 0
        inside = [o for o in out if o[0] <= dur + 5]
        covered = (max((o[0] for o in inside), default=0) >= dur * 0.5) if dur else True
        if len(inside) < 10 or not covered:
            print(f"[!] 字幕时间戳越界且剩余太少（{len(out)} 段里只有 {len(inside)} 段可用），跳过",
                  file=sys.stderr)
            continue
        if len(inside) < len(out):
            print(f"[!] 字幕比视频长，已裁掉越界的 {len(out) - len(inside)} 段", file=sys.stderr)

        # 护栏二：内容得和标题沾边（拦「时间戳合法但整段是别人视频」）
        if not title_matches(info["title"], "".join(t for _, t in inside)):
            print("[!] 字幕内容和标题对不上，按无字幕处理", file=sys.stderr)
            continue

        return inside
    return []


def title_matches(title: str, text: str) -> bool:
    """标题里的实词，至少有一个出现在正文里（宽松判断，避免误杀）。

    中文标题没法按空格切词，这里退一步：取标题里所有 2 字片段，
    只要有一个出现在正文中就认。宁可放过，不要误杀真字幕。
    """
    probe = text[:4000]
    grams = set()
    for chunk in re.split(r"[^\w\u4e00-\u9fff]+", title):
        chunk = re.sub(r"[0-9A-Za-z_]+", "", chunk)
        for i in range(len(chunk) - 1):
            g = chunk[i:i + 2]
            if CJK.match(g[0]) and g not in STOP:
                grams.add(g)
    if not grams:
        return True
    return any(g in probe for g in grams)


def download_audio(info: dict, cookies: dict) -> str:
    """用 playurl 接口拿音频直链，省掉 yt-dlp 依赖。"""
    j = _get("https://api.bilibili.com/x/player/playurl",
             params={"bvid": info["bvid"], "cid": info["cid"], "fnval": 16, "qn": 16},
             cookies=cookies).json()
    if j.get("code") != 0:
        raise NoteError(f"取音频地址失败：{j.get('code')} {j.get('message')}")
    dash = (j.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        raise NoteError("这个视频没有可下载的音频轨。")

    url = audios[0].get("baseUrl") or audios[0].get("base_url")
    if not url:
        raise NoteError("音频直链是空的。")

    tmp = os.environ.get("BILINOTE_TMP") or os.path.join(HOME, "work")
    os.makedirs(tmp, exist_ok=True)
    dst = os.path.join(tmp, f"{info['bvid']}.m4a")

    r = requests.get(url, headers={**HEAD, "Range": "bytes=0-"}, stream=True,
                     cookies=cookies, timeout=60)
    if r.status_code not in (200, 206):
        raise NoteError(f"下载音频失败（HTTP {r.status_code}）。")
    with open(dst, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return dst


def transcribe(info: dict, model_size: str, cookies: dict) -> list[tuple[float, str]]:
    """本地转写：playurl 下音频 → faster-whisper 出时间戳。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise NoteError("没装 faster-whisper。装一下再跑：pip install faster-whisper")

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    audio = download_audio(info, cookies)
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio, language="zh", vad_filter=True, beam_size=5)
        return [(float(s.start), s.text.strip()) for s in segs]
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass


# ── 文本处理 ────────────────────────────────────────────────────────────
def mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT = "。！？，、；：,.!?;:"

# 中文里出现频率极高但没有信息量的词（用来把"废话 n-gram"过滤掉）
STOP = {
    "这个", "那个", "我们", "你们", "他们", "什么", "怎么", "就是", "然后",
    "因为", "所以", "但是", "可以", "一个", "这样", "那样", "现在", "时候",
    "如果", "还是", "已经", "不是", "没有", "自己", "知道", "觉得", "其实",
    "非常", "真的", "一下", "一些", "很多", "这种", "那种", "而且", "或者",
}


def tidy(text: str) -> str:
    t = re.sub(r"\s+", "", text)
    if not t:
        return ""
    if t[-1] not in PUNCT:
        t += "。"
    return t


def to_sentences(segments: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """按时间空档切句：停顿超过 0.55 秒算一句结束。"""
    out: list[tuple[float, str]] = []
    buf, start, prev_end = [], 0.0, None
    for t, text in segments:
        text = tidy(text)
        if not text:
            continue
        if not buf:
            start = t
        buf.append(text)
        gap = t - prev_end if prev_end is not None else None
        prev_end = t
        if gap is not None and gap > 0.55:
            out.append((start, "".join(buf)))
            buf = []
    if buf:
        out.append((start, "".join(buf)))
    return out


def _clauses(sentence: str) -> list[str]:
    return [c.strip() for c in re.split(r"[，、；：,;:]", sentence) if c.strip()]


def summarize(sentences: list[tuple[float, str]], top_n: int = 5) -> list[tuple[float, str]]:
    """抽取式摘要：按「短语（2–4 字）重复度」打分。

    比按字频好一点：先按标点切成小句，只在句子层面比较，避免长句被字数撑高。
    """
    if len(sentences) <= top_n:
        return sentences

    freq: dict[str, int] = {}
    for _, s in sentences:
        for clause in _clauses(s):
            for n in (2, 3):
                for i in range(len(clause) - n + 1):
                    g = clause[i:i + n]
                    if not CJK.match(g[0]):
                        continue
                    if g in STOP:
                        continue
                    freq[g] = freq.get(g, 0) + 1

    scored = []
    for idx, (t, s) in enumerate(sentences):
        score = 0.0
        hit = 0
        for clause in _clauses(s):
            for n in (2, 3):
                for i in range(len(clause) - n + 1):
                    g = clause[i:i + n]
                    c = freq.get(g, 0)
                    if c > 1:
                        score += c
                        hit += 1
        if hit:
            score = score / (hit ** 0.5)
        scored.append((score, idx, t, s))

    picked = sorted(sorted(scored, key=lambda x: -x[0])[:top_n], key=lambda x: x[1])
    return [(t, s) for _, _, t, s in picked]


def build_note(info: dict, segments: list[tuple[float, str]], source: str) -> str:
    sentences = to_sentences(segments)
    lines = [f"# {info['title']}", ""]
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


# ── 主流程 ──────────────────────────────────────────────────────────────
def one(target: str, cookies: dict, args) -> str:
    bv = resolve_target(target)
    info = video_info(bv, cookies)
    print(f"[*] {info['title']}（{mmss(info['duration'])}）", file=sys.stderr)

    t0 = time.time()
    segs = fetch_subtitle(info, cookies)
    source = "B站字幕轨"
    if not segs:
        if args.no_asr:
            raise NoteError("这条没有可信字幕轨；去掉 --no-asr 可本地转写。")
        print("[*] 没有可信字幕，开始本地转写（要几分钟）…", file=sys.stderr)
        segs = transcribe(info, args.model, cookies)
        source = f"本地转写（whisper-{args.model}）"
    if not segs:
        raise NoteError("没拿到任何文本。")
    print(f"[*] 用时 {time.time() - t0:.1f}s，{len(segs)} 段", file=sys.stderr)

    return build_note(info, segs, source)


def main() -> int:
    ap = argparse.ArgumentParser(description="B站视频 → 文字笔记")
    ap.add_argument("targets", nargs="+", help="BV 号或视频链接，可以给多个")
    ap.add_argument("-o", "--out", help="输出文件；给多个视频时当作目录")
    ap.add_argument("--model", default="small", help="Whisper 模型：tiny/base/small/medium")
    ap.add_argument("--no-asr", action="store_true", help="没字幕就报错，不做本地转写")
    args = ap.parse_args()

    cookies = load_cookies()
    out_is_dir = bool(args.out) and (len(args.targets) > 1 or args.out.endswith(("/", "\\")))
    if out_is_dir:
        os.makedirs(args.out, exist_ok=True)

    failed = []
    for i, target in enumerate(args.targets, 1):
        tag = f"[{i}/{len(args.targets)}] " if len(args.targets) > 1 else ""
        try:
            note = one(target, cookies, args)
        except NoteError as e:
            print(f"{tag}✗ {target}：{e}", file=sys.stderr)
            failed.append(target)
            continue
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return 130

        if out_is_dir:
            bv = resolve_target(target)
            path = os.path.join(args.out, f"{bv}.md")
        else:
            path = args.out

        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(note)
            print(f"{tag}→ {path}", file=sys.stderr)
        else:
            print(note)

    if failed:
        print(f"\n有 {len(failed)} 条没成功：{', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NoteError as e:
        print(f"出错：{e}", file=sys.stderr)
        raise SystemExit(1)
