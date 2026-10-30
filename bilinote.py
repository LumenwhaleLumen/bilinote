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

        # 护栏二：内容得和标题沾边（拦「时间戳合法但整段是别人视频」）。
        # 2026-10-29 改：这条只做「提醒」，不再直接拒——实测两边都会误判：
        #   · 猪实验那类：正文是叙事，标题里的"科学家/深海/实验"根本不会念出来 → 误杀；
        #   · 蚂蝗混进 harness 视频那类：标题词也确实不出现 → 该拦但拦不住靠词。
        # 所以重叠度低时保留字幕、在笔记里标一句，让人自己核对，比默默丢掉好。
        if not title_matches(info["title"], "".join(t for _, t in inside)):
            print("[!] 字幕和标题字面重合度低，可能是错配——已保留，笔记里会标出来",
                  file=sys.stderr)
            info["subtitle_warn"] = True

        return inside
    return []


def title_matches(title: str, text: str) -> bool:
    """标题和正文是否沾边——用比例判断，不用"命中一个字就算"。

    2026-10-29 改：旧版只要标题里任意一个 2 字片段出现就算过，反过来只要
    一个字都没出现在正文前 4000 字里就判死，会误杀真字幕（比如标题是
    "海底10000米…"、正文里"海底"出现在 4000 字之后）。现在看两个比例：
      · 标题 2 字片段在全文里的命中率 ≥ 15%
      · 标题汉字在全文里的覆盖率 ≥ 40%
    满足任一即认。标题没有汉字（外语标题）时不检查。
    """
    probe = text[:20000]
    low = probe.lower()

    # 先看标题里的拉丁词（DeepSeek / Harness / VSCode…）：这类词区分度最好，
    # 只要有一个出现在正文里就够；标题有拉丁词但一个都没出现，直接判不沾边。
    latin = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", title)]
    if latin:
        return any(w in low for w in latin)

    chunks = [re.sub(r"[0-9A-Za-z_]+", "", c)
              for c in re.split(r"[^\w\u4e00-\u9fff]+", title)]
    chunks = [c for c in chunks if c]

    grams, chars = set(), set()
    for c in chunks:
        for i in range(len(c) - 1):
            g = c[i:i + 2]
            # 只留"有区分度"的片段：泛词（是什么/到底/背后…）命中率天然很高，
            # 留着会让任何一份错配字幕都通过检查
            if CJK.match(g[0]) and g not in STOP_BIGRAMS:
                grams.add(g)
        for ch in c:
            if CJK.match(ch):
                chars.add(ch)

    if not grams:
        return True  # 标题只剩泛词（比如"这是什么"），无从比较，放过

    gram_hit = sum(1 for g in grams if g in probe) / len(grams)
    char_hit = (sum(1 for ch in chars if ch in probe) / len(chars)) if chars else 0.0
    return gram_hit >= 0.25 or char_hit >= 0.60


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

# 标题里常见但没区分度的 2 字片段（"是什么/到底/背后/运作"这类，几乎每个
# 视频都会出现）。判断标题与正文是否沾边时必须先剔除，否则任何字幕都能蒙混过关。
STOP_BIGRAMS = STOP | {
    "是什", "么是", "到底", "为什", "背后", "运作", "怎么", "怎样", "如何",
    "这是", "的什", "什东", "东西", "到底", "哪些", "哪种", "几个", "一些", "以及",
    "这样", "那样", "不是", "就是", "都会", "会怎", "么样", "看看", "一起",
}


def tidy(text: str) -> str:
    """清空白，但**不补句号**——补句号是整句的事，不是每个片段的事。"""
    return re.sub(r"\s+", "", text)


def _finish(sentence: str) -> str:
    """给一整句收尾：末尾没标点才补一个句号。"""
    if not sentence:
        return ""
    if sentence[-1] not in PUNCT:
        sentence += "。"
    return sentence


def to_sentences(segments: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """按时间空档切句：停顿超过 0.55 秒算一句结束。

    2026-10-30 修（阿汐的测试抓出来的）：原来先 append 再判空档，导致
    「新一句的开头」被吞进上一句，句子越切越少；而且每个片段都补句号，
    拼起来一句里出现好几个句号。现在顺序反过来：先判空档再 append，
    句号只在整句结束时补一次。
    """
    out: list[tuple[float, str]] = []
    buf: list[str] = []
    start = 0.0
    prev_end: float | None = None

    for t, text in segments:
        text = tidy(text)
        if not text:
            continue

        gap = (t - prev_end) if prev_end is not None else None
        # 空档够长 → 先把上一句收掉，再开新句
        if buf and gap is not None and gap > 0.55:
            out.append((start, _finish("".join(buf))))
            buf = []

        if not buf:
            start = t
        buf.append(text)
        prev_end = t

    if buf:
        out.append((start, _finish("".join(buf))))
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
    if info.get("subtitle_warn"):
        lines.append("- ⚠️ 字幕与标题字面重合度低，**可能不是这条视频的字幕**，请对照视频核对")
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

    # 没有句子就别渲染空的「## 正文」（2026-10-30 修：阿汐的测试抓到的）
    if sentences:
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
    ap.add_argument("-j", "--jobs", type=int, default=3,
                    help="并行处理的条数（默认 3；本地转写很吃 CPU，转写多时建议调小到 1-2）")
    args = ap.parse_args()

    cookies = load_cookies()
    out_is_dir = bool(args.out) and (len(args.targets) > 1 or args.out.endswith(("/", "\\")))
    if out_is_dir:
        os.makedirs(args.out, exist_ok=True)

    total = len(args.targets)
    jobs = max(1, min(args.jobs, total))
    failed: list[str] = []

    def work(idx: int, target: str):
        tag = f"[{idx}/{total}] " if total > 1 else ""
        try:
            note = one(target, cookies, args)
        except NoteError as e:
            print(f"{tag}✗ {target}：{e}", file=sys.stderr)
            return target, None
        # 输出放在工作线程里也没问题：每个文件互不相同，stderr 打印也是行级的
        path = None
        if out_is_dir:
            path = os.path.join(args.out, f"{resolve_target(target)}.md")
        elif args.out:
            path = args.out
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(note)
            print(f"{tag}→ {path}", file=sys.stderr)
        else:
            print(note)
        return target, note

    if jobs == 1 or total == 1:
        for i, t in enumerate(args.targets, 1):
            target, _ = work(i, t)
            if _ is None:
                failed.append(target)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(work, i, t) for i, t in enumerate(args.targets, 1)]
            for fut in as_completed(futures):
                try:
                    target, note = fut.result()
                except KeyboardInterrupt:
                    print("\n已中断。", file=sys.stderr)
                    return 130
                if note is None:
                    failed.append(target)

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
