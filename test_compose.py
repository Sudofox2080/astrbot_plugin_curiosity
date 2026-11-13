# -*- coding: utf-8 -*-
"""在 AstrBot 进程外，用真实 deepseek-chat-search provider 验证 _compose 口吻。

关键：必须先 import astrbot.api.star 建立依赖链，否则 ProviderManager 循环导入。
依赖构造签名（AstrBot 4.16）：
  SQLiteDatabase(db_path) -> await initialize()
  SharedPreferences(db)   -> UmopConfigRouter(...)
  AstrBotConfigManager(default_config, ucr, sp)
  PersonaManager(db, acm)
  ProviderManager(acm, db, persona_mgr)
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import astrbot.api.star  # noqa: F401  先建依赖链
from astrbot.core import AstrBotConfig
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.umop_config_router import UmopConfigRouter
from astrbot.core.utils.shared_preferences import SharedPreferences

PLUGIN = "/opt/astrbot/data/plugins/astrbot_plugin_curiosity/main.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("cur_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def main():
    mod = load_plugin()

    db = SQLiteDatabase("/opt/astrbot/data/data_v4.db")
    await db.initialize()
    sp = SharedPreferences(db)
    ucr = UmopConfigRouter(sp)
    acm = AstrBotConfigManager(AstrBotConfig("/opt/astrbot/data/cmd_config.json"), ucr, sp)
    pmgr = PersonaManager(db, acm)
    pm = ProviderManager(acm, db, pmgr)
    await pm.initialize()

    prov = await pm.get_provider_by_id("openai/deepseek-chat-search")
    print("provider 拿到:", prov is not None)
    if prov is None:
        print("可用:", list(getattr(pm, "prov_inst_map", {}).keys())[:15])
        return

    p = mod.CuriosityPlugin.__new__(mod.CuriosityPlugin)
    p.context = type("C", (), {"get_provider_by_id": lambda self, x: prov})()
    p.config = {
        "max_items_per_run": 3,
        "search_timeout_seconds": 120,
        "persona_hint": (
            "你是一条叫本鱼的鲸鱼娘，嘴硬心软，说话直接、带点漫不经心，"
            "讲东西喜欢用生活化的比喻，讨厌又臭又长的官腔。"
        ),
    }
    p._db = None
    p._task = None
    p._data_dir = Path("/tmp/curiosity_test2")
    await p._db_open()

    p._db.execute(
        "INSERT INTO finds(fp,title,gist,why,url,topic,score,created_at) VALUES"
        "('fp1','谷歌发布 Genie 4 世界模型','能通过文字实时生成可交互的虚拟环境',"
        "'以后玩游戏能自己描述场景','https://x.com/a','测试',9,0)"
    )
    p._db.execute(
        "INSERT INTO finds(fp,title,gist,why,url,topic,score,created_at) VALUES"
        "('fp2','英伟达拟向 Meta 投资 30 亿美元','算力和模型深度绑定的标志性事件',"
        "'钱多到能改写行业格局','https://x.com/b','测试',7,0)"
    )
    p._db.commit()

    rows = p._take_ready(4)
    print("取出:", len(rows), "条")

    txt = await p._compose(rows)
    print("=== 组织后的话 ===")
    print(txt)

    p._db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
