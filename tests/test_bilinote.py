"""bilinote 的回归测试。

只测纯逻辑与护栏，不联网：需要网络的函数一律把 `bilinote._get`
或 `requests.get` 换成假实现。

跑法：
    python -m pytest tests/ -q
"""
from unittest import mock

import pytest
import requests

import bilinote


# ── 小工具 ────────────────────────────────────────────────────────────────
class FakeResp:
    """假的 requests.Response，只要 .json() / .status_code / .url。"""

    def __init__(self, payload=None, status_code=200, url=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.url = url

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def info(**kw):
    base = {
        "bvid": "BV1xx411c7mD",
        "aid": 1,
        "cid": 2,
        "title": "深海热液喷口为什么能养活一整个群落",
        "author": "某UP",
        "duration": 600,
        "desc": "",
    }
    base.update(kw)
    return base


# ── 时间与文本 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "sec,expect",
    [(0, "00:00"), (59, "00:59"), (60, "01:00"), (61, "01:01"),
     (3599, "59:59"), (3600, "60:00"), (3661, "61:01")],
)
def test_mmss(sec, expect):
    assert bilinote.mmss(sec) == expect


def test_mmss_rounds_down_float():
    # 处理视频时长时经常给浮点秒，不能出现 "00:60"
    assert bilinote.mmss(59.99) == "00:59"


def test_tidy_only_strips_whitespace():
    # 2026-10-30 起 tidy 不补句号（补标点统一由 to_sentences 的 _finish 负责）
    assert bilinote.tidy("  你好  世界 ") == "你好世界"
    assert bilinote.tidy("已经带句号。") == "已经带句号。"
    assert bilinote.tidy("问句？") == "问句？"
    assert bilinote.tidy("   ") == ""


def test_to_sentences_splits_on_pause():
    segs = [(0.0, "第一句"), (0.4, "还没断"), (1.2, "第二句")]
    out = bilinote.to_sentences(segs)
    assert [t for _, t in out] == ["第一句还没断。", "第二句。"]
    assert out[0][0] == 0.0 and out[1][0] == 1.2


def test_to_sentences_does_not_insert_period_mid_sentence():
    out = bilinote.to_sentences([(0.0, "前半句"), (0.3, "后半句")])
    assert len(out) == 1
    assert out[0][1].count("。") == 1, f"同一句里出现了多个句号：{out[0][1]!r}"


def test_to_sentences_single_long_segment():
    out = bilinote.to_sentences([(0.0, "一口气说完不换气")])
    assert len(out) == 1 and out[0] == (0.0, "一口气说完不换气。")


def test_to_sentences_drops_blank_after_tidy():
    out = bilinote.to_sentences([(0.0, "  "), (1.0, "有内容")])
    assert [t for _, t in out] == ["有内容。"]


def test_clauses_splits_on_punctuation():
    assert bilinote._clauses("甲，乙；丙：丁,戊") == ["甲", "乙", "丙", "丁", "戊"]


# ── 摘要 ──────────────────────────────────────────────────────────────────
def test_summarize_returns_input_when_short():
    sents = [(0.0, "一。"), (1.0, "二。")]
    assert bilinote.summarize(sents, top_n=5) == sents


def test_summarize_prefers_repeated_content():
    sents = [
        (0.0, "热液喷口附近有大量化能细菌。"),
        (10.0, "这里的水温非常高。"),
        (20.0, "热液喷口附近的细菌养活了管虫。"),
        (30.0, "今天天气不错。"),
        (40.0, "热液喷口附近还有虾。"),
        (50.0, "随便一句没有重复的话。"),
    ]
    picked = bilinote.summarize(sents, top_n=3)
    assert len(picked) == 3
    # 时间顺序必须保持
    assert [t for t, _ in picked] == sorted(t for t, _ in picked)
    picked_text = "".join(s for _, s in picked)
    assert "热液喷口" in picked_text


# ── 笔记渲染 ──────────────────────────────────────────────────────────────
def test_build_note_contains_metadata_and_body():
    segs = [(0.0, "开头一句。"), (5.0, "第二句。")]
    md = bilinote.build_note(info(), segs, "官方字幕")
    assert md.startswith("# 深海热液喷口为什么能养活一整个群落")
    assert "- UP：某UP" in md
    assert "- 时长：10:00" in md
    assert "https://www.bilibili.com/video/BV1xx411c7mD" in md
    assert "- 文本来源：官方字幕" in md
    assert "[00:00]" in md
    assert "共 " in md  # 末尾统计行，具体句数受切句缺陷影响，不锁死


def test_build_note_marks_suspicious_subtitle():
    md = bilinote.build_note(info(subtitle_warn=True), [(0.0, "内容。")], "官方字幕")
    assert "可能不是这条视频的字幕" in md


def test_build_note_quotes_description():
    md = bilinote.build_note(info(desc="第一行\n第二行"), [(0.0, "内容。")], "官方字幕")
    assert "> 第一行" in md and "> 第二行" in md


def test_build_note_without_segments_skips_empty_section():
    md = bilinote.build_note(info(), [], "官方字幕")
    assert "## 正文" not in md
    assert "## 要点" not in md


# ── 输入解析 ──────────────────────────────────────────────────────────────
def test_resolve_target_bv():
    assert bilinote.resolve_target("BV1xx411c7mD") == "BV1xx411c7mD"


def test_resolve_target_full_url():
    url = "https://www.bilibili.com/video/BV1xx411c7mD/?p=1"
    assert bilinote.resolve_target(url) == "BV1xx411c7mD"


def test_resolve_target_rejects_garbage():
    with pytest.raises(bilinote.NoteError):
        bilinote.resolve_target("这不是BV号")


def test_resolve_target_rejects_url_without_bv():
    with pytest.raises(bilinote.NoteError):
        bilinote.resolve_target("https://www.bilibili.com/read/cv123")


def test_resolve_target_short_link():
    with mock.patch.object(bilinote.requests, "get",
                           return_value=FakeResp(url="https://www.bilibili.com/video/BV1xx411c7mD")):
        assert bilinote.resolve_target("https://b23.tv/abcdefg") == "BV1xx411c7mD"


def test_resolve_target_short_link_dead():
    with mock.patch.object(bilinote.requests, "get", return_value=FakeResp(url="https://b23.tv/x")):
        with pytest.raises(bilinote.NoteError, match="短链"):
            bilinote.resolve_target("https://b23.tv/abcdefg")


# ── 标题与正文是否沾边 ────────────────────────────────────────────────────
def test_title_matches_latin_word_present():
    assert bilinote.title_matches("DeepSeek Harness 插件评测", "今天我们聊 deepseek harness 的插件")


def test_title_matches_latin_word_absent():
    assert not bilinote.title_matches("DeepSeek Harness 插件评测", "这是一段完全无关的讲做菜的文本")


def test_title_matches_chinese_overlap():
    body = "深海热液喷口附近有化能细菌，喷口温度很高。"
    assert bilinote.title_matches("深海热液喷口", body)


def test_title_matches_chinese_mismatch():
    body = "今天教大家做红烧肉，先准备五花肉和冰糖。"
    assert not bilinote.title_matches("深海热液喷口为什么能养活群落", body)


def test_title_matches_only_stopwords_passes():
    # 标题只剩泛词时无从比较，应放过（否则真字幕会被误杀）
    assert bilinote.title_matches("是什么", "任意内容都行")


# ── 字幕护栏 ──────────────────────────────────────────────────────────────
def _sub_payload(segs, duration=600):
    """构造 /x/player/v2 与字幕 json 的组合。"""
    body = {"body": [{"from": f, "content": c} for f, c in segs]}
    return body


def test_fetch_subtitle_no_track_returns_empty():
    fake = FakeResp({"data": {"subtitle": {"subtitles": []}}})
    with mock.patch.object(bilinote, "_get", return_value=fake):
        assert bilinote.fetch_subtitle(info(), {}) == []


def test_fetch_subtitle_skips_music_placeholder():
    segs = [(float(i), "♪音乐♪") for i in range(30)]
    player = FakeResp({"data": {"subtitle": {"subtitles": [
        {"subtitle_url": "//sub.example/x.json"}]}}})
    sub = FakeResp(_sub_payload(segs))

    def fake_get(url, **kw):
        return player if "player" in url else sub

    with mock.patch.object(bilinote, "_get", side_effect=fake_get):
        assert bilinote.fetch_subtitle(info(), {}) == []


def _segments(n, step=3.0):
    return [(i * step, f"第{i}句话的内容") for i in range(n)]


def test_fetch_subtitle_trims_overlong_track():
    # 视频 60 秒，字幕却到 300 秒：应裁掉越界段，保留并告警
    segs = _segments(100, step=3.0)  # 0..297 秒
    player = FakeResp({"data": {"subtitle": {"subtitles": [
        {"subtitle_url": "//sub.example/x.json"}]}}})
    sub = FakeResp(_sub_payload(segs))
    with mock.patch.object(bilinote, "_get",
                           side_effect=lambda url, **kw: player if "player" in url else sub):
        got = bilinote.fetch_subtitle(info(duration=60), {})
    assert got, "越界但仍有足够覆盖时不该整条丢掉"
    assert max(t for t, _ in got) <= 65


def test_fetch_subtitle_rejects_too_short():
    segs = _segments(5, step=1.0)
    player = FakeResp({"data": {"subtitle": {"subtitles": [
        {"subtitle_url": "//sub.example/x.json"}]}}})
    sub = FakeResp(_sub_payload(segs))
    with mock.patch.object(bilinote, "_get",
                           side_effect=lambda url, **kw: player if "player" in url else sub):
        assert bilinote.fetch_subtitle(info(duration=600), {}) == []


def test_fetch_subtitle_warns_on_title_mismatch_but_keeps():
    segs = _segments(40, step=10.0)  # 覆盖 0..390 秒
    player = FakeResp({"data": {"subtitle": {"subtitles": [
        {"subtitle_url": "//sub.example/x.json"}]}}})
    sub = FakeResp(_sub_payload([(t, "今天教大家做红烧肉") for t, _ in segs]))
    i = info(duration=600, title="深海热液喷口为什么能养活群落")
    with mock.patch.object(bilinote, "_get",
                           side_effect=lambda url, **kw: player if "player" in url else sub):
        got = bilinote.fetch_subtitle(i, {})
    assert got, "重合度低只该标记，不该整条丢弃"
    assert i.get("subtitle_warn") is True


# ── 网络层错误 ────────────────────────────────────────────────────────────
def test_get_raises_friendly_error_on_403():
    with mock.patch.object(bilinote.requests, "get",
                           return_value=FakeResp({}, status_code=403)):
        with mock.patch.object(bilinote.time, "sleep"):
            with pytest.raises(bilinote.NoteError, match="403"):
                bilinote._get("https://api.bilibili.com/x")


def test_get_retries_server_error_then_raises():
    calls = []
    def fake_get(url, **kw):
        calls.append(1)
        return FakeResp({}, status_code=503)
    with mock.patch.object(bilinote.requests, "get", side_effect=fake_get):
        with mock.patch.object(bilinote.time, "sleep"):
            with pytest.raises(bilinote.NoteError, match="503"):
                bilinote._get("https://api.bilibili.com/x")
    assert len(calls) == 3


def test_video_info_raises_on_api_code():
    with mock.patch.object(bilinote, "_get",
                           return_value=FakeResp({"code": -404, "message": "啥都木有"})):
        with pytest.raises(bilinote.NoteError, match="啥都木有"):
            bilinote.video_info("BV1xx411c7mD", {})
