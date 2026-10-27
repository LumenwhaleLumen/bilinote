# bilinote

把 B 站视频转成**带时间戳的 Markdown 笔记**。

优先用视频自带的字幕轨（秒出）；没有字幕、或字幕被判为不可信时，回退到本地 faster-whisper 转写。全程不调用大模型，纯本地跑，免费。

## 它做什么

输入 BV 号或视频链接，输出一份 `.md`：

- 标题、UP 主、时长、来源链接、文本来源（字幕 / 本地转写）
- 抽取式要点（按短语重复度打分，不调模型）
- 按停顿断句、合并成段、**每段带时间戳**的正文

适合：看长视频前先扫一眼结构、把讲座/纪录片存成可检索的文字、做资料整理。

## 安装

```bash
pip install requests              # 必需
pip install faster-whisper        # 只在需要本地转写时
```

**不再需要 yt-dlp**：音频直接用 B站 `playurl` 接口取。

## 用法

```bash
python bilinote.py BV1xxxxxxxxx
python bilinote.py https://www.bilibili.com/video/BV1xxxxxxxxx -o note.md
python bilinote.py https://b23.tv/xxxxxx                      # 短链也行
python bilinote.py BV1aa BV1bb https://b23.tv/ccc -o notes/   # 批量，按 BV 号落盘
python bilinote.py BV1xx --model medium                       # 换更大的 Whisper 模型
python bilinote.py BV1xx --no-asr                             # 没字幕就报错，不等转写
```

- 多个输入时 `-o` 当目录用（以 `/` 结尾也会当目录）
- 出错只打印一句人话，不打堆栈；批量时某条失败不影响其它条

## 踩过的坑（都写进代码了）

1. **有字幕轨 ≠ 有内容**：有些视频的字幕整轨是 `♪音乐♪` 占位行，需要按实词比例判断，不够就回退转写。
2. **字幕可能比视频长**：AI 字幕轨有时覆盖的是另一个剪辑版本（实测 428 段里只有 108 段落在片长内）。现在会裁掉越界部分，只要剩下的够长且覆盖到片尾就认。
3. **字幕整段可能是别的视频的内容**：实测一条深海视频拿到的是「瓜摊段子」字幕，时间戳完全合法。所以加了内容护栏——标题里的字词得在正文里出现，否则按无字幕处理。
4. **接口有数据 ≠ 数据对**：以上三条都是真跑出来的，不是猜的。

## 已知限制

- 需要联网取 B 站接口；字幕轨通常要求登录态（读 `~/.dsh/bili-duty/bili_credential.json`，读不到就匿名跑）
- 本地转写准确度取决于模型大小与音质
- 弹幕、评论、画面内容都不处理
- 中文摘要用短语频率，没有分词库，效果比真正的 NLP 摘要弱

## License

MIT
