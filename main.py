"""群聊关键词唤醒：消息正文出现指定关键词时，按概率让机器人接话。

设计要点：
- 逻辑挂在 CustomFilter 上，AstrBot 在**唤醒阶段**就会调用它；
  filter 返回 True 等于"唤醒这条消息"，随后由模型阶段正常生成回复。
- 关键词匹配、概率、冷却都在模型之前完成，未命中时**零 token 消耗**。
- 只看纯文本段：@ 别人的昵称里带关键词不算命中。
- 同一个群冷却 N 秒；同一条消息只掷一次骰子（按 message_id 缓存）。
"""

from __future__ import annotations

import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

DEFAULTS = {
    "enabled": True,
    "keyword": "",
    "probability": 0.5,
    "cooldown_seconds": 30,
    "group_whitelist": [],
    "bot_ids": [],
}

# 插件实例读到配置后写进这里；filter 在唤醒阶段执行，那时还没有实例上下文
RUNTIME = {
    "conf": dict(DEFAULTS),
    "last_fire": {},   # group_id -> 上次真正接话的时间戳
    "decisions": {},   # message_id -> 本次是否接话
}


def _conf() -> dict:
    return RUNTIME["conf"]


def _plain_text(event: AstrMessageEvent) -> str:
    """只取纯文本段做关键词匹配。

    At 组件不算文本：否则 @ 一个昵称里带关键词的机器人时，
    message_str 会渲染成 "@昵称"，把别人的 @ 误判成自己的关键词。
    """
    try:
        messages = event.get_messages()
    except Exception:
        return event.message_str or ""
    parts = []
    for comp in messages:
        if isinstance(comp, Plain):
            parts.append(getattr(comp, "text", "") or "")
    return "".join(parts)


def _sender_is_bot(event: AstrMessageEvent, bot_ids: set[str]) -> bool:
    """判断发送者是不是机器人。

    OneBot v11 没有统一的机器人标记，所以同时看三个来源：
    raw sender 上的 is_bot 字段、配置里的黑名单、以及发送者 ID。
    """
    if str(event.get_sender_id()) in bot_ids:
        return True

    raw = getattr(event.message_obj, "raw_message", None)
    sender = None
    if isinstance(raw, dict):
        sender = raw.get("sender")
    elif raw is not None:
        sender = getattr(raw, "sender", None)

    if isinstance(sender, dict):
        if sender.get("is_bot"):
            return True
        if str(sender.get("user_id", "")) in bot_ids:
            return True
    return False


def evaluate(event: AstrMessageEvent) -> bool:
    """返回 True 表示这次应当唤醒模型去接话。"""
    conf = _conf()
    if not conf.get("enabled", True):
        return False

    # 只在群聊生效；私聊本来就每条都回
    if event.is_private_chat():
        return False

    group_id = str(event.get_group_id() or "")
    whitelist = [str(g) for g in (conf.get("group_whitelist") or [])]
    if whitelist and group_id not in whitelist:
        return False

    message_id = str(getattr(event.message_obj, "message_id", "") or "")
    decisions = RUNTIME["decisions"]
    if message_id and message_id in decisions:
        return decisions[message_id]

    def decide(value: bool) -> bool:
        if message_id:
            decisions[message_id] = value
            if len(decisions) > 512:
                decisions.pop(next(iter(decisions)))
        return value

    if str(event.get_sender_id()) == str(event.get_self_id()):
        return decide(False)

    bot_ids = {str(b) for b in (conf.get("bot_ids") or [])}
    if _sender_is_bot(event, bot_ids):
        return decide(False)

    keyword = str(conf.get("keyword") or "").strip()
    if not keyword:
        return decide(False)
    if keyword.lower() not in _plain_text(event).lower():
        return decide(False)

    cooldown = int(conf.get("cooldown_seconds") or 0)
    last = RUNTIME["last_fire"].get(group_id, 0.0)
    if cooldown > 0 and time.time() - last < cooldown:
        return decide(False)

    try:
        probability = float(conf.get("probability", 0.5))
    except (TypeError, ValueError):
        probability = 0.5
    if random.random() >= max(0.0, min(1.0, probability)):
        return decide(False)

    RUNTIME["last_fire"][group_id] = time.time()
    # 只设 is_wake 不够：ProcessStage 还要求 is_at_or_wake_command 才会进模型
    event.is_at_or_wake_command = True
    logger.info(
        f"[keyword_wake] 命中关键词，接话：group={group_id} sender={event.get_sender_id()}"
    )
    return decide(True)


class KeywordWakeFilter(filter.CustomFilter):
    """唤醒阶段的自定义过滤器（AstrBot 会传入 raise_error 实例化）。"""

    def __init__(self, raise_error: bool = True) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            return evaluate(event)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能影响其他消息
            logger.error(f"[keyword_wake] filter 异常: {exc}")
            return False


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        conf = dict(DEFAULTS)
        conf.update(config or {})
        RUNTIME["conf"] = conf
        logger.info(
            f"[keyword_wake] 已加载：keyword={conf['keyword']!r} "
            f"probability={conf['probability']} cooldown={conf['cooldown_seconds']}s"
        )

    @filter.custom_filter(KeywordWakeFilter)
    async def on_keyword(self, event: AstrMessageEvent):
        """命中的消息只做唤醒标记，回复交给 AstrBot 的模型阶段。"""
        event.is_at_or_wake_command = True
        event.is_wake = True
        yield
