"""URL recognition: the pasted-text shapes real users produce.

These cases are lifted from what the desktop app had to accept (share messages
with a title and brackets, ``b23.tv`` short links, bare BV ids) plus the
Markdown/link-syntax shapes an agent chat introduces -- the trailing ``)`` case
is exactly the kind of thing that silently returns "no video found" in a
hand-written parser.
"""
from __future__ import annotations

import pytest

from pickvedio_core.errors import PickvedioError
from pickvedio_core.urls import (
    classify,
    extract_bilibili_urls,
    extract_video_urls,
    find_all_video_urls,
    validate_video_url,
)

CANON = "https://www.bilibili.com/video/BV1rjKn6CETw/"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CANON, CANON),
        (CANON + "?spm_id_from=333.788.recommend_more_video.0", CANON),
        ("https://bilibili.com/video/BV1rjKn6CETw", CANON),
        ("【硬核】大学物理 强杀电磁波 https://www.bilibili.com/video/BV1rjKn6CETw/ 值得一看", CANON),
        ("看看这个 av17280074 哈哈", "https://www.bilibili.com/video/av17280074/"),
        ("BV1rjKn6CETw", CANON),
        ("bv1rjKn6CETw", CANON),
        ("https://www.bilibili.com/video/BV1rjKn6CETw/?p=3", CANON),
        ("https://www.bilibili.com/bangumi/play/ep123456?bvid=BV1rjKn6CETw", CANON),
        ("https://b23.tv/GUAy8Zm", "https://b23.tv/GUAy8Zm"),
    ],
)
def test_bilibili_shapes_normalise(text, expected):
    assert extract_bilibili_urls(text)[0] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL0", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ],
)
def test_youtube_shapes_normalise(text, expected):
    assert extract_video_urls(text)[0] == expected


def test_markdown_link_is_not_broken_by_the_closing_paren():
    text = "推荐 [一个视频](https://youtu.be/dQw4w9WgXcQ) 很好看"
    assert extract_video_urls(text) == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


def test_trailing_chinese_punctuation_is_stripped():
    assert extract_video_urls("看这个 https://b23.tv/AbC123，很神") == ["https://b23.tv/AbC123"]


def test_multiple_links_keep_reading_order_and_dedupe():
    text = (
        "先讲 " + CANON + " 再补充 https://www.bilibili.com/video/BV1rjKn6CETw/ 以及 "
        "https://youtu.be/dQw4w9WgXcQ"
    )
    urls = extract_video_urls(text)
    assert urls == [CANON, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


@pytest.mark.parametrize(
    "text",
    ["", "完全没有链接", "https://example.com/article/123", "BV123", "https://youtu.be/short", "av"],
)
def test_non_video_text_is_rejected_not_guessed(text):
    assert extract_video_urls(text) == []
    with pytest.raises(PickvedioError) as caught:
        validate_video_url(text)
    assert "未识别" in str(caught.value)


def test_unknown_platform_is_refused_unless_opted_in():
    url = "https://v.douyin.com/iAbCdEf/"
    with pytest.raises(PickvedioError):
        validate_video_url(url)
    assert validate_video_url(url, allow_unknown_platform=True) == url


def test_classify_yields_dispatch_identity():
    assert classify(CANON).video_id == "BV1rjKn6CETw"
    assert classify(CANON).platform == "bilibili"
    ref = classify("https://youtu.be/dQw4w9WgXcQ")
    assert (ref.platform, ref.video_id) == ("youtube", "dQw4w9WgXcQ")
    assert ref.identity == "youtube:dQw4w9WgXcQ"
    short = classify("https://b23.tv/GUAy8Zm")
    assert short.platform == "bilibili" and short.extra == {"short_link": True}


def test_find_all_video_urls_requires_allow_flag_for_other_hosts():
    text = CANON + " 还有 https://example.com/video.mp4"
    refs = find_all_video_urls(text)
    assert [item.platform for item in refs] == ["bilibili"]
    broad = find_all_video_urls(text, allow_unknown_platform=True)
    assert {item.platform for item in broad} == {"bilibili", "other"}


def test_bvid_pattern_rejects_wrong_length():
    assert extract_bilibili_urls("BV1rjKn6CET") == []
    assert extract_bilibili_urls("BV1rjKn6CETw") != []
