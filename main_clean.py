# -*- coding: utf-8 -*-
"""好奇心 —— 让 bot 自己上网翻新东西，看见有意思的就攒着，回头讲给你听。

工作方式：
    1. 定时（默认每 3 小时）挑一个感兴趣的方向，调 provider 的联网搜索
    2. 把搜到的东西拆成「一条一条」，存进本地 SQLite
    3. 按 URL / 标题指纹去重，同一件事不重复攒
    4. 攒够 N 条（或到了问候时段）主动讲给主人听
    5. 讲过的标记 delivered，永远不会再讲第二遍

它不是搜索引擎的包装，是一个「会自己产生谈资」的东西：
    有话想说时才开口，没攒到真正有趣的就不开口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

# AstrBot 的 filter.command 只认 event.message_str（已被 wake_prefix 剥过的那份）。
# 但 wake_prefix 未必是 "/"，有人配成 ["imbot77"]，此时 "/讲讲" 永远命中不了指令，
# 会被当成普通聊天丢给 LLM。这里自己写一个过滤器兜底：剥掉 wake_prefix 之后，
# 再允许任意常见指令标记（/ ! 。 空格，或者干脆什么都不加）。
_CMD_STRIP = " \t\r\n/\uff0f!\uff01.\u3002\u3001,\uff0c:\uff1a~\uff5e"


def _strip_cmd(text: str, wake_prefixes: list[str] | None = None) -> str:
    """把消息前面的 wake_prefix 和常见指令标记都剥掉。"""
    s = (text or "").strip()
    changed = True
    while changed:
        changed = False
        for p in (wake_prefixes or []):
            if p and s.startswith(p):
                s = s[len(p):]
                changed = True
        s2 = s.lstrip(_CMD_STRIP)
        if s2 != s:
            s = s2
            changed = True
    return s.strip()


class _CmdFilter(filter.CustomFilter):
    """匹配 `讲讲` / `/讲讲` / `imbot77讲讲` / `imbot77 /讲讲` 等各种写法。

    注意：AstrBot 的 `filter.custom_filter` 只把 kwargs 转给 `get_handler_or_create`，
    **不会**转给过滤器构造器。所以 names 不能走构造参数，必须做成类属性
    （见 `_cmd()`：每次动态创建子类并把 names 钉在类上）。
    """

    names: tuple = ()

    def _candidates(self, event: AstrMessageEvent) -> list[str]:
        raw = event.message_str or ""
        cands = [raw, _strip_cmd(raw, None)]
        try:
            wps = event.get_extra("_wake_prefixes")
        except Exception:  # noqa: BLE001
            wps = None
        if wps:
            cands.append(_strip_cmd(raw, wps))
        seen, out = set(), []
        for c in cands:
            c = (c or "").strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _match(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """命中了返回 (剩余参数, 指令名)。"""
        for c in self._candidates(event):
            for name in self.names:
                if c == name:
                    return "", name
                if c.startswith(name + " ") or c.startswith(name + "\u3000"):
                    return c[len(name):].strip(), name
        return None

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        if not event.is_private_chat() and not event.is_at_or_wake_command:
            return False
        hit = self._match(event)
        if not hit:
            return False
        left, name = hit
        # 指令函数里用 event.get_extra("cmd_args") 取参数
        event.set_extra("cmd_args", left)
        event.set_extra("cmd_name", name)
        return True


def _cmd(*names: str):
    """注册一个宽匹配指令：动态子类，把 names 钉在类属性上。"""
    cls = type("_CmdFilter_" + "_".join(names), (_CmdFilter,), {"names": tuple(names)})
    return filter.custom_filter(cls)


SCHEMA = """
CREATE TABLE IF NOT EXISTS finds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fp          TEXT UNIQUE NOT NULL,   -- 指纹，去重用
    title       TEXT NOT NULL,
    gist        TEXT NOT NULL,          -- 一句话说清这是什么
    why         TEXT NOT NULL,          -- 为什么有意思
    url         TEXT,
    topic       TEXT,                   -- 哪个方向翻到的
    score       INTEGER DEFAULT 5,      -- 有趣程度 1-10
    created_at  REAL NOT NULL,
    delivered   INTEGER DEFAULT 0,      -- 是否已经讲给老大听过
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_delivered ON finds(delivered, created_at);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT,
    at         REAL NOT NULL,
    ok         INTEGER,
    got        INTEGER DEFAULT 0,
    fresh      INTEGER DEFAULT 0,       -- 去重后真正新增的
    elapsed    REAL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS prefs (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

DEFAULT_TOPICS = [
    "AI 大模型最近有什么新发布或新能力",
    "开源软件、开发工具最近有什么值得关注的新项目",
    "科技圈最近有什么有意思的技术突破",
    "有趣的冷知识、科学发现",
    "独立开发者、小团队做出的有意思的东西",
]


def _now() -> float:
    return time.time()


def _log_prefix() -> str:
    return f"[{PLUGIN_NAME}]"


PLUGIN_NAME = "astrbot_plugin_curiosity"


def _strip_md(text: str) -> str:
    """去掉 markdown 装饰，留下干净的话。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_STOPWORDS = (
    "发布", "推出", "宣布", "上线", "公开", "更新", "支持", "新增", "全新",
    "最新", "正式", "重磅", "首次", "首个", "全球", "业内", "官网", "报道",
    "模型", "版本", "系列", "功能", "能力", "性能", "消息", "据悉", "透露",
)


def _norm_title(title: str) -> str:
    """把标题压成去重用的骨架：丢标点、丢版本噪声、丢通用词。

    deepseek-chat-search 返回的是散文总结，几乎没有 URL 可抓，
    所以去重只能靠标题。但同一件事两次搜索会被写成
    「Kimi K3.1 Pro：3万亿参数全球最大开源模型」和
    「月之暗面开源 Kimi K3.1 Pro，参数达 3 万亿」——
    直接比标题必然漏重，必须先洗成骨架。
    """
    t = (title or "").lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    for w in _STOPWORDS:
        t = t.replace(w, "")
    return t


def _fingerprint(title: str, url: str = "") -> str:
    base = (url or "").strip().lower()
    if not base:
        base = _norm_title(title)
        if not base:
            base = re.sub(r"\s+", "", (title or "").lower())
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


def _title_loose(title: str) -> str:
    """宽骨架：只留「字母数字汉字」，用于近似判重查询。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (title or "").lower())


# 硬专名：英文/数字型号串（Kimi K3.1、Genie 4、GPT-6 Astra、LFM3、V4.1 Flash…）
# 和带中文的机构+产品组合。这些是「同一件事」的真正锚点，
# 描述性词语（发布/支持/首次/最强）不是。
_ANCHOR_EN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-_ ]?[A-Za-z0-9]+)*")
_ANCHOR_CN_ORG = re.compile(
    r"(月之暗面|深度求索|谷歌|Google|Meta|英伟达|NVIDIA|OpenAI|Anthropic|"
    r"腾讯|阿里|字节|百度|智谱|MiniMax|Mistral|LiquidAI|微软|Amazon|Apple)"
)
# 太通用的英文词不算专名
_ANCHOR_STOP = {
    "ai", "the", "and", "for", "new", "pro", "max", "mini", "flash", "plus",
    "ultra", "light", "beta", "alpha", "api", "app", "web", "dev", "llm",
    # 单位、量纲、常见缩略语——出现频率高但没有辨识度，撞上不算重复
    "gb", "tb", "mb", "kb", "mhz", "ghz", "fps", "ms", "kw", "mah", "w",
    "b", "k", "m", "g", "t", "v", "x",
}

# 同一实体的不同写法，归一到同一个锚点，否则「Google」和「谷歌」撞不上
_ANCHOR_ALIAS = {
    "google": "谷歌", "googledeepmind": "谷歌", "deepmind": "谷歌",
    "gemini": "google gemini",
    "microsoft": "微软", "openai": "openai", "meta": "meta",
    "nvidia": "英伟达", "moonshot": "月之暗面", "deepseek": "深度求索",
}

# 这些锚点太泛，单独撞上不能证明是同一件事
# （谷歌昨天发 Gemini、今天发 Genie，都是「谷歌」但完全两码事）
_SWEEP_ANCHORS = {
    "谷歌", "openai", "meta", "英伟达", "微软", "月之暗面", "深度求索",
    "腾讯", "阿里", "字节", "百度", "智谱", "minimax", "mistral", "liquidai",
    "amazon", "apple", "google gemini",
}


def _anchors(title: str, gist: str = "") -> set[str]:
    """抽出「硬专名」集合，用于跨标题判断是不是同一件事。

    「Kimi K3.1 Pro以2.8万亿参数登顶」和「月之暗面Kimi K3.1 Pro开源」
    描述同一件事的两个侧面，二元组相似度只有 0.2 出头过不了线；
    但两边都有 kimi k3.1 pro 这个锚点，一撞就知道重复。

    注意锚点必须**够硬**：单位（GB）、通用技术词（AI、API）、
    泛化词（发布、支持）都不能算，否则会大面积误判。

    **只看标题，不看 gist。** gist 里经常拿别的产品做对比
    （「超越 GPT-6 Astra」「仅次于 Claude Opus」），
    那些名字属于别人，拿进来会把不相干的两条判成重复。
    gist 参数保留只为调用方签名兼容。
    """
    text = title or ""
    out: set[str] = set()

    for m in _ANCHOR_EN.finditer(text):
        tok = m.group(0).strip(" .-_").lower()
        if len(tok) < 2:
            continue
        if tok in _ANCHOR_STOP:
            continue
        # 纯数字（年份、金额、百分比）不是专名
        if re.fullmatch(r"[0-9.\-_ ]+", tok):
            continue
        # 去掉量纲尾巴：1GB / 30亿 里的单位不参与
        tok = re.sub(r"\b\d+\s*(gb|tb|mb|kb|mhz|ghz)\b", "", tok).strip()
        if not tok or tok in _ANCHOR_STOP:
            continue

        # 关键：把空格分隔的多词串拆成单锚点并逐一收录。
        # 「DeepMind Genie 4」必须同时产出 deepmind / genie 4，
        # 否则它和「Genie 4」完全撞不上——这是之前漏判的真正原因。
        # 同时保留整串，供「Kimi K3.1 Pro」这类必须连读的产品名使用。
        parts = tok.split()
        if len(parts) > 1:
            out.add(_ANCHOR_ALIAS.get(tok, tok))
            for p in parts:
                if len(p) < 2 or p in _ANCHOR_STOP:
                    continue
                if re.fullmatch(r"[0-9.\-_]+", p):
                    continue
                out.add(_ANCHOR_ALIAS.get(p, p))
            # 相邻词两两拼一起：「genie 4」「kimi k3.1」「k3.1 pro」
            for i in range(len(parts) - 1):
                pair = f"{parts[i]} {parts[i + 1]}"
                if len(pair) >= 3:
                    out.add(_ANCHOR_ALIAS.get(pair, pair))
        else:
            out.add(_ANCHOR_ALIAS.get(tok, tok))

    for m in _ANCHOR_CN_ORG.finditer(text):
        out.add(_ANCHOR_ALIAS.get(m.group(1).lower(), m.group(1).lower()))

    return {a for a in out if a and a not in _ANCHOR_STOP}


class CuriosityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._db: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    @property
    def interval_hours(self) -> float:
        try:
            return max(0.25, float(self._cfg("interval_hours", 3)))
        except (TypeError, ValueError):
            return 3.0

    @property
    def provider_id(self) -> str:
        return str(self._cfg("search_provider_id", "openai/deepseek-chat-search") or "")

    @property
    def timeout_seconds(self) -> int:
        try:
            return max(10, int(self._cfg("search_timeout_seconds", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def target_umo(self) -> str:
        return str(self._cfg("target_umo", "") or "").strip()

    @property
    def deliver_threshold(self) -> int:
        try:
            return max(1, int(self._cfg("deliver_threshold", 4)))
        except (TypeError, ValueError):
            return 4

    @property
    def quiet_hours(self) -> tuple[int, int]:
        """免打扰时段（小时），默认 1 点到 8 点不讲。"""
        raw = self._cfg("quiet_hours", [1, 8])
        try:
            a, b = int(raw[0]), int(raw[1])
            return a, b
        except (TypeError, ValueError, IndexError):
            return 1, 8

    @property
    def topics(self) -> list[str]:
        raw = self._cfg("topics", None)
        if isinstance(raw, list) and raw:
            items = [str(x).strip() for x in raw if str(x).strip()]
            if items:
                return items
        return list(DEFAULT_TOPICS)

    @property
    def max_items_per_run(self) -> int:
        try:
            return max(1, min(10, int(self._cfg("max_items_per_run", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def persona_hint(self) -> str:
        return str(
            self._cfg(
                "persona_hint",
                "你是一条叫本鱼的鲸鱼娘，嘴硬心软，说话直接、带点漫不经心，"
                "讲东西喜欢用生活化的比喻，讨厌又臭又长的官腔。",
            )
        )

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    async def _db_open(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "curiosity.db"
        conn = sqlite3.connect(
            path,
            check_same_thread=False,
            timeout=10.0,  # Windows 上 SQLite 的文件锁等待更短，给足重试余量
        )
        conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互相阻塞，跨平台行为一致，Windows 上尤其明显
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 开 WAL 失败（不影响使用）：{exc}")
        conn.executescript(SCHEMA)
        conn.commit()
        self._db = conn
        logger.info(f"{_log_prefix()} 数据库就绪 {path}")
        return conn

    def _pref_get(self, k: str, default: Any = None) -> Any:
        if self._db is None:
            return default
        row = self._db.execute("SELECT v FROM prefs WHERE k=?", (k,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, json.JSONDecodeError):
            return row["v"]

    def _pref_set(self, k: str, v: Any) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO prefs(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # 搜
    # ------------------------------------------------------------------
    async def _get_provider(self):
        prov_id = self.provider_id
        for getter in (
            lambda: self.context.get_provider_by_id(prov_id),
            lambda: self.context.provider_manager.get_provider_by_id(prov_id),
        ):
            try:
                provider = getter()
            except Exception:  # noqa: BLE001
                provider = None
            if asyncio.iscoroutine(provider):
                provider = await provider
            if provider is not None:
                return provider
        return None

    async def _raw_search(self, query: str) -> dict[str, Any]:
        provider = await self._get_provider()
        if provider is None:
            return {"ok": False, "error": f"未找到搜索提供商：{self.provider_id}"}
        started = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=query, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"搜索超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"搜索失败：{exc}"}
        text = getattr(resp, "completion_text", None) or str(resp or "")
        return {
            "ok": True,
            "text": text.strip(),
            "elapsed": round(time.time() - started, 2),
        }

    async def _extract(self, topic: str, raw_text: str) -> list[dict[str, Any]]:
        """把一篇搜索结果的原文，拆成结构化的「一条一条」。"""
        provider = await self._get_provider()
        if provider is None:
            return []

        prompt = f"""下面是一段联网搜索返回的资料，主题是「{topic}」。

请从里面挑出最多 {self.max_items_per_run} 个**真正有意思、值得讲给人听**的点。
宁缺毋滥——如果整段都没什么新鲜的，就返回空数组。

每个点给出：
- title: 标题，10-30 字，**必须把专有名词（公司名、产品名、型号）原样留在里面**，
  不要写成「某公司发布新模型」这种去掉了主体、下次认不出来的标题
- gist: 一句话说清它是什么，不要超过 60 字
- why: 为什么有意思、值得一听，一句话，不超过 50 字
- url: 如果能从原文里找到链接就填，找不到就填空字符串
- score: 1-10，你觉得有多值得讲

只输出 JSON 数组，不要任何解释和其他文字。

资料原文：
{raw_text[:6000]}"""

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 抽取失败：{exc}")
            return []

        text = getattr(resp, "completion_text", None) or str(resp or "")
        text = _strip_md(text)
        # 容错：有时会包 ```json
        m = re.search(r"\[.*\]", text, flags=re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []

        items: list[dict[str, Any]] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            gist = str(it.get("gist") or "").strip()
            if not title or not gist:
                continue
            try:
                score = int(it.get("score") or 5)
            except (TypeError, ValueError):
                score = 5
            items.append(
                {
                    "title": title[:120],
                    "gist": gist[:200],
                    "why": str(it.get("why") or "").strip()[:160],
                    "url": str(it.get("url") or "").strip()[:400],
                    "score": max(1, min(10, score)),
                }
            )
        return items

    def _is_dup(
        self, conn: sqlite3.Connection, title: str, gist: str = ""
    ) -> bool:
        """近似判重：标题二元组相似度 OR 「专名共现」任一命中，就当同一件事。

        为什么只比标题不够：
          「Kimi K3.1 Pro以2.8万亿参数成全球最大开源模型」
          「月之暗面Kimi K3.1 Pro开源，3D建模得分92.7%」
        标题描述的是同一件事的不同侧面，二元组杰卡德只有 0.2 出头，
        但两句话里都出现了同一个**硬专名**（Kimi K3.1 Pro）。
        专名才是「同一件事」的真正锚点，描述词不是。

        所以判重分两层：
          1. 标题骨架二元组杰卡德 ≥ 0.28（应对换句话重写同一描述）
          2. 标题里至少 1 个硬专名两边都有（应对同事件不同侧面）
        """
        new = _title_loose(title)
        if len(new) < 4:
            return False
        new_grams = {new[i : i + 2] for i in range(len(new) - 1)}
        if not new_grams:
            return False
        new_anchors = _anchors(title, gist)

        rows = conn.execute(
            "SELECT title, gist FROM finds ORDER BY id DESC LIMIT 500"
        ).fetchall()
        for row in rows:
            old = _title_loose(row["title"])
            if len(old) < 4:
                continue
            if new == old:
                return True

            # 层 1：硬专名共现。专名是同一件事的铁锚，命中即重复。
            # 但「谷歌」「meta」这类机构名太泛，单独撞上不算：
            # 谷歌今天发 Genie、昨天发 Gemini，都是「谷歌」却完全不是一件事。
            # 所以要求重叠里至少有一个是**具体的**（含数字，或不是机构名）。
            if new_anchors:
                old_anchors = _anchors(row["title"], row["gist"] or "")
                common = new_anchors & old_anchors
                if any(a not in _SWEEP_ANCHORS for a in common):
                    return True

            # 层 2：一方完整包含另一方，且短的一方够长
            shorter, longer = (new, old) if len(new) <= len(old) else (old, new)
            if len(shorter) >= 8 and shorter in longer:
                return True

            # 层 3：二元组杰卡德相似度
            old_grams = {old[i : i + 2] for i in range(len(old) - 1)}
            union = len(new_grams | old_grams)
            if union and len(new_grams & old_grams) / union >= 0.28:
                return True
        return False


    async def _run_once(self, topic: str | None = None) -> dict[str, Any]:
        """完整跑一轮：挑方向 → 搜 → 拆 → 存。"""
        conn = await self._db_open()
        if topic is None:
            topic = random.choice(self.topics)

        started = time.time()
        res = await self._raw_search(topic)
        if not res.get("ok"):
            conn.execute(
                "INSERT INTO runs(topic,at,ok,got,fresh,elapsed,note) VALUES(?,?,?,?,?,?,?)",
                (topic, _now(), 0, 0, 0, round(time.time() - started, 2), res.get("error", "")),
            )
            conn.commit()
            logger.warning(f"{_log_prefix()} 本轮失败 topic={topic!r} {res.get('error')}")
            return {"ok": False, "error": res.get("error"), "topic": topic, "fresh": 0}

        items = await self._extract(topic, res.get("text", ""))
        fresh = 0
        for it in items:
            fp = _fingerprint(it["title"], it.get("url", ""))
            # 第二道闸：骨架哈希之外，再按宽骨架做一次近似判重。
            # 上游是散文总结、没有 URL，同一件事换句话写就会绕过哈希，
            # 所以必须再加一层「包含关系」检查。
            if self._is_dup(conn, it["title"]):
                continue
            try:
                conn.execute(
                    """INSERT INTO finds(fp,title,gist,why,url,topic,score,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        fp,
                        it["title"],
                        it["gist"],
                        it["why"],
                        it.get("url", ""),
                        topic,
                        it["score"],
                        _now(),
                    ),
                )
                fresh += 1
            except sqlite3.IntegrityError:
                continue  # 老的，跳过
        conn.execute(
            "INSERT INTO runs(topic,at,ok,got,fresh,elapsed,note) VALUES(?,?,?,?,?,?,?)",
            (topic, _now(), 1, len(items), fresh, round(time.time() - started, 2), ""),
        )
        conn.commit()
        logger.info(
            f"{_log_prefix()} 本轮完成 topic={topic!r} 抽出={len(items)} 新增={fresh} "
            f"用时={res.get('elapsed')}s"
        )
        return {"ok": True, "topic": topic, "got": len(items), "fresh": fresh}

    # ------------------------------------------------------------------
    # 讲
    # ------------------------------------------------------------------
    def _count_ready(self) -> int:
        if self._db is None:
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM finds WHERE delivered=0"
        ).fetchone()
        return int(row["n"]) if row else 0

    def _take_ready(self, limit: int, prefer_keyword: str = "") -> list[sqlite3.Row]:
        if self._db is None:
            return []
        kw = (prefer_keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            rows = self._db.execute(
                """SELECT * FROM finds WHERE delivered=0
                   AND (title LIKE ? OR gist LIKE ? OR topic LIKE ?)
                   ORDER BY score DESC, created_at DESC LIMIT ?""",
                (like, like, like, limit),
            ).fetchall()
            if rows:
                return list(rows)
        rows = self._db.execute(
            """SELECT * FROM finds WHERE delivered=0 ORDER BY score DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return list(rows)

    def _mark_delivered(self, find_id: int) -> None:
        if self._db is None:
            return
        self._db.execute(
            "UPDATE finds SET delivered=1, delivered_at=? WHERE id=?",
            (_now(), find_id),
        )
        self._db.commit()

    def _in_quiet_hours(self) -> bool:
        a, b = self.quiet_hours
        h = datetime.now().hour
        if a <= b:
            return a <= h < b
        return h >= a or h < b

    async def _compose(self, rows: list[sqlite3.Row]) -> str:
        """让模型用自己的口吻把这几条讲出来。"""
        provider = await self._get_provider()
        if provider is None:
            return ""

        lines = []
        for i, r in enumerate(rows, 1):
            seg = f"{i}. 标题：{r['title']}\n   是什么：{r['gist']}\n   为什么有意思：{r['why']}"
            if (r["url"] or "").strip():
                seg += f"\n   链接：{r['url']}"
            lines.append(seg)
        body = "\n".join(lines)

        prompt = f"""{self.persona_hint}

你刚才自己上网翻了点东西，攒了这么几条，现在要讲给老大听。
不要写成资讯播报，不要列 1 2 3 4 这种条目，用你自己说话的语气，自然地说给他听。
可以挑最有意思的一两条讲透，剩下的带一句。
总长度控制在 200 字以内，别长篇大论。

攒到的东西：
{body}"""

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 组织语言失败：{exc}")
            return ""

        text = getattr(resp, "completion_text", None) or str(resp or "")
        return _strip_md(text).strip()

    async def _deliver(self, force: bool = False) -> dict[str, Any]:
        conn = await self._db_open()
        umo = self.target_umo
        if not umo:
            return {"ok": False, "error": "未配置 target_umo"}

        ready = self._count_ready()
        if ready <= 0:
            return {"ok": False, "error": "还没攒到新东西"}
        if not force:
            if ready < self.deliver_threshold:
                return {"ok": False, "error": f"只攒了 {ready} 条，不到 {self.deliver_threshold} 条"}
            if self._in_quiet_hours():
                return {"ok": False, "error": "免打扰时段"}

        rows = self._take_ready(self.deliver_threshold)
        if not rows:
            return {"ok": False, "error": "没取到条目"}

        text = await self._compose(rows)
        if not text:
            return {"ok": False, "error": "组织语言失败"}

        try:
            sent = await self._send(umo, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 发送失败：{exc}")
            return {"ok": False, "error": f"发送失败：{exc}"}
        if not sent:
            return {"ok": False, "error": "没有可用的活跃平台会话"}

        ids = [r["id"] for r in rows]
        qs = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE finds SET delivered=1, delivered_at=? WHERE id IN ({qs})",
            (_now(), *ids),
        )
        conn.commit()
        logger.info(f"{_log_prefix()} 已讲给老大，{len(ids)} 条，umo={umo}")
        return {"ok": True, "delivered": len(ids), "text": text, "umo": umo}

    async def _send(self, umo: str, text: str) -> bool:
        """按 umo 找到平台实例，主动发一条消息。

        AstrBot 4.16 的实际 API（踩过坑，记在这里）：
          - 平台实例挂在 `platform_manager.platform_insts`（list），
            **没有** `get_instances()` 这个方法
          - 平台 id 用 `inst.meta().id` 取，不是 `platform_name`
          - aiocqhttp 的 client 是 `inst.get_client()` 返回的 CQHttp，
            它的签名是 `call_action(action: str, **params)`

        不用 `context.send_message`，因为那是被动回复，需要 event 上下文；
        这里是定时主动发，必须直接走底层 action。
        """
        platform_id, _, rest = umo.partition(":")
        msg_type = "friend"
        target_id = ""
        if ":" in rest:
            kind, _, target_id = rest.partition(":")
            msg_type = "group" if "Group" in kind else "friend"
        else:
            target_id = rest

        inst = None
        getter = getattr(self.context, "get_platform_inst", None)
        if callable(getter):
            try:
                inst = getter(platform_id)
            except Exception:  # noqa: BLE001
                inst = None
        if inst is None:
            insts = getattr(self.context.platform_manager, "platform_insts", None) or []
            if not insts:
                getter2 = getattr(self.context.platform_manager, "get_insts", None)
                if callable(getter2):
                    insts = getter2() or []
            for cand in insts:
                try:
                    pid = cand.meta().id
                except Exception:  # noqa: BLE001
                    pid = ""
                if str(pid) == platform_id:
                    inst = cand
                    break
        if inst is None:
            logger.warning(f"{_log_prefix()} 找不到平台实例 {platform_id}")
            return False

        client = None
        getter_c = getattr(inst, "get_client", None)
        if callable(getter_c):
            client = getter_c()
        if client is None:
            client = getattr(inst, "bot", None)
        if client is None:
            logger.warning(f"{_log_prefix()} 平台 {platform_id} 拿不到 client")
            return False

        try:
            if msg_type == "group":
                await client.call_action(
                    "send_group_msg", group_id=int(target_id), message=text
                )
            else:
                await client.call_action(
                    "send_private_msg", user_id=int(target_id), message=text
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 发送到 {umo} 失败：{exc}")
        return False

    # ------------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        # 启动后先等一会，避开 AstrBot 冷启动
        await asyncio.sleep(90)
        while True:
            try:
                if not self.enabled:
                    await asyncio.sleep(600)
                    continue
                last = float(self._pref_get("last_run", 0) or 0)
                gap = self.interval_hours * 3600
                if _now() - last >= gap:
                    await self._run_once()
                    self._pref_set("last_run", _now())
                # 每次翻完，看看要不要讲
                if self._count_ready() >= self.deliver_threshold and not self._in_quiet_hours():
                    await self._deliver()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{_log_prefix()} 循环出错：{exc}")
            await asyncio.sleep(600)

    # ------------------------------------------------------------------
    # LLM 工具（自然语言触发，不靠指令）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="tell_me_something_interesting")
    async def tool_tell(self, event: AstrMessageEvent, topic_hint: str = "") -> str:
        """从自己网上攒下来的东西里挑有意思的讲出来。

        当对方问「有什么新鲜事」「最近看到啥」「讲讲你翻到的东西」「有什么好玩的」，
        或者聊天时明显想听你分享见闻、而不是要查资料时，调用这个。
        不要用它回答事实性问题（那种情况用搜索工具）。

        Args:
            topic_hint(string): 对方想听哪方面的，比如「科技」「AI」「随便」。留空就是随便挑。
        """
        try:
            await self._db_open()
        except Exception as exc:  # noqa: BLE001
            return f"数据库没打开：{exc}"

        hint = (topic_hint or "").strip()
        rows = self._take_ready(1, prefer_keyword=hint) if hint else self._take_ready(1)
        if not rows:
            return (
                "兜里现在空的，还没攒到新东西。可以告诉对方你这就去翻翻，"
                "或者说刚才翻过了没碰到什么有意思的，别编。"
            )
        r = rows[0]
        # 取一条并标记已讲，但不走主动推送通道（这次是对话内直接回答）
        self._mark_delivered(r["id"])
        return (
            f"你要讲的这半分钟就是这条：\n"
            f"- 标题：{r['title']}\n"
            f"- 一句话概括：{r['gist']}\n"
            f"- 为什么值得讲：{r['why']}\n"
            f"{('- 出处：' + r['url']) if r.get('url') else ''}\n"
            f"\n用自己的语气讲给对方听：口语、短句、带点你自己的看法，"
            f"像随口聊到的一样，不要列条目、不要写成资讯播报、不要说「这条新闻」这种词。"
        )

    @filter.llm_tool(name="check_curiosity_backlog")
    async def tool_backlog(self, event: AstrMessageEvent, unused: str = "") -> str:
        """看看自己攒了多少还没讲的东西。

        当对方问「你攒了多少」「手头有啥存货」「最近翻到几样东西」时调用。

        Args:
            unused(string): 这个参数没用，传空字符串即可。
        """
        await self._db_open()
        ready = self._count_ready()
        total = self._db.execute("SELECT COUNT(*) AS n FROM finds").fetchone()["n"]
        if not ready:
            return f"累计攒下 {total} 条，没讲的有 0 条。"
        rows = self._take_ready(min(ready, 5))
        lines = [f"累计攒下 {total} 条，没讲的有 {ready} 条。其中几条是："]
        for r in rows:
            lines.append(f"- {r['title']}（{r['gist']}）")
        return "\n".join(lines)

    @filter.llm_tool(name="go_find_something")
    async def tool_find(self, event: AstrMessageEvent, direction: str = "") -> str:
        """立刻联网翻一轮新东西，把有意思的攒下来。

        只有对方明确让你「现在去翻」「去找点新的」「查最新的」时才调用。
        这个要花几秒到几十秒，别随便调。

        Args:
            direction(string): 想让你翻哪个方向，比如「AI 模型」「太空」「生物」。留空就按自己的兴趣挑。
        """
        try:
            res = await self._run_once((direction or "").strip() or None)
        except Exception as exc:  # noqa: BLE001
            return f"翻的时候出错了：{exc}"
        if not res.get("ok"):
            return f"没翻成：{res.get('error')}"
        return (
            f"翻了「{res['topic']}」，抽出 {res.get('got', 0)} 条，新增 {res.get('fresh', 0)} 条。"
            f"（重复的会自动丢掉，不用重复讲。）"
        )

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    def _args(self, event: AstrMessageEvent) -> str:
        """取指令后面的参数（兼容 wake_prefix 不是 / 的情况）。"""
        got = event.get_extra("cmd_args")
        if isinstance(got, str):
            return got.strip()
        name = event.get_extra("cmd_name") or ""
        s = _strip_cmd(event.message_str or "")
        if name and s.startswith(name):
            s = s[len(name):]
        return s.strip()

    @_cmd("翻翻")
    async def cmd_browse(self, event: AstrMessageEvent):
        """立刻出去翻一轮。"""
        topic = self._args(event) or None
        yield event.plain_result("我出去翻一下……")
        res = await self._run_once(topic)
        if not res.get("ok"):
            yield event.plain_result(f"没翻成：{res.get('error')}")
            return
        yield event.plain_result(
            f"翻了「{res['topic']}」，抽出 {res.get('got', 0)} 条，新增 {res.get('fresh', 0)} 条。"
        )

    @_cmd("攒了什么")
    async def cmd_backlog(self, event: AstrMessageEvent):
        """看看攒了多少还没讲的。"""
        await self._db_open()
        ready = self._count_ready()
        rows = self._take_ready(8)
        if not rows:
            yield event.plain_result("现在手里空的，还没攒到新东西。")
            return
        lines = [f"没讲的有 {ready} 条："]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r['title']} —— {r['gist']}（{r['score']}分）")
        yield event.plain_result("\n".join(lines))

    @_cmd("讲讲")
    async def cmd_tell(self, event: AstrMessageEvent):
        """马上讲给他听。"""
        res = await self._deliver(force=True)
        if not res.get("ok"):
            yield event.plain_result(f"讲不了：{res.get('error')}")
            return
        yield event.plain_result(f"（{res['delivered']} 条已发到 {res['umo']}）\n{res['text']}")

    @_cmd("翻翻状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看看它最近翻得怎么样。"""
        await self._db_open()
        total = self._db.execute("SELECT COUNT(*) AS n FROM finds").fetchone()["n"]
        ready = self._count_ready()
        runs = self._db.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 5").fetchall()
        lines = [
            f"启用：{'开' if self.enabled else '关'}",
            f"节奏：每 {self.interval_hours} 小时翻一次",
            f"累计攒下：{total} 条 / 没讲：{ready} 条",
            f"讲给谁：{self.target_umo or '（没设）'}",
        ]
        for r in runs:
            t = datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            lines.append(
                f"· {t} {r['topic']} → {'成' if r['ok'] else '败'} 抽{r['got']} 新{r['fresh']} {r['elapsed'] or 0}s"
            )
        yield event.plain_result("\n".join(lines))

    @_cmd("清空攒的")
    async def cmd_clear(self, event: AstrMessageEvent):
        """把没讲的都扔掉。"""
        await self._db_open()
        n = self._count_ready()
        self._db.execute("DELETE FROM finds WHERE delivered=0")
        self._db.commit()
        yield event.plain_result(f"扔了 {n} 条。")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        await self._db_open()
        if self.enabled:
            self._task = asyncio.create_task(self._loop())
        logger.info(
            f"{_log_prefix()} 已加载；启用={self.enabled} 节奏={self.interval_hours}h "
            f"讲给={self.target_umo or '未设置'} 让讲门限={self.deliver_threshold}条"
        )

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._db:
            try:
                # Windows 上文件句柄没释放会让重装/升级时删不掉目录
                self._db.commit()
            except Exception:  # noqa: BLE001
                pass
            self._db.close()
            self._db = None
        logger.info(f"{_log_prefix()} 已卸载")
