import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Star, StarTools, register
from astrbot.core.provider.provider import Provider


OUTCOME_ACCEPTED = "accepted"
OUTCOME_TOO_FULL = "too_full"
OUTCOME_DISLIKED = "disliked"
OUTCOME_UNKNOWN = "unknown"


@dataclass(slots=True)
class FoodItem:
    name: str
    aliases: tuple[str, ...]
    satiety_gain: float
    favorability_gain: float
    mood_gain: float
    preference: str
    note: str
    accept_hint: str
    refuse_hint: str


@register(
    "PetFeeder",
    "Codex",
    "可配置食物、饱食度和 LLM 反应的喂食插件",
    "1.1.0",
    "https://github.com/yourname/astrbot-plugin-feed-pet",
)
class PetFeederPlugin(Star):
    # 主流程是“读当前会话状态 -> 应用衰减 -> 判断能不能吃 -> 更新状态 -> 生成反应”。
    def __init__(self, context: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(str(StarTools.get_data_dir()))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "feed_state.json"
        self._lock = asyncio.Lock()
        self._data = self._load_data()

    # ---- 配置解析 ----
    def _get_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _coerce_int(self, value: Any, default: int, minimum: int = 0) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, parsed)

    def _coerce_float(self, value: Any, default: float, minimum: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, parsed)

    def _get_int(self, key: str, default: int, minimum: int = 0) -> int:
        return self._coerce_int(self.config.get(key, default), default, minimum)

    def _get_float(self, key: str, default: float, minimum: float = 0.0) -> float:
        return self._coerce_float(self.config.get(key, default), default, minimum)

    def _get_text(self, key: str, default: str = "") -> str:
        value = str(self.config.get(key, default) or "").strip()
        return value or default

    def _parse_text_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        normalized = (
            text.replace("\r", "\n")
            .replace(",", "\n")
            .replace("，", "\n")
            .replace("、", "\n")
        )
        return [line.strip() for line in normalized.split("\n") if line.strip()]

    def _format_number(self, value: float) -> str:
        rounded = round(float(value), 1)
        if abs(rounded - int(rounded)) < 1e-9:
            return str(int(rounded))
        return f"{rounded:.1f}"

    def _enabled(self) -> bool:
        return self._get_bool("enabled", True)

    def _ignore_self_messages(self) -> bool:
        return self._get_bool("ignore_self_messages", True)

    def _allow_private_chat(self) -> bool:
        return self._get_bool("allow_private_chat", True)

    def _allow_reset_command(self) -> bool:
        return self._get_bool("allow_reset_command", True)

    def _reset_requires_admin(self) -> bool:
        return self._get_bool("reset_requires_admin", True)

    def _require_admin_to_feed(self) -> bool:
        return self._get_bool("require_admin_to_feed", False)

    def _allowed_user_ids(self) -> list[str]:
        return self._parse_text_list(self.config.get("allowed_user_ids", ""))

    def _blocked_user_ids(self) -> list[str]:
        return self._parse_text_list(self.config.get("blocked_user_ids", ""))

    def _feed_cooldown_seconds(self) -> float:
        return self._get_float("feed_cooldown_seconds", 30.0, minimum=0.0)

    def _pet_name(self) -> str:
        return self._get_text("pet_name", "团子")

    def _initial_satiety(self) -> float:
        return float(self._get_int("initial_satiety", 20, minimum=0))

    def _max_satiety(self) -> float:
        return float(self._get_int("max_satiety", 100, minimum=1))

    def _refuse_threshold(self) -> float:
        return min(self._get_float("refuse_threshold", 90.0, minimum=0.0), self._max_satiety())

    def _satiety_decay_per_hour(self) -> float:
        return self._get_float("satiety_decay_per_hour", 6.0, minimum=0.0)

    def _initial_favorability(self) -> float:
        return float(self._get_int("initial_favorability", 50, minimum=0))

    def _max_favorability(self) -> float:
        return float(self._get_int("max_favorability", 100, minimum=1))

    def _initial_mood(self) -> float:
        return float(self._get_int("initial_mood", 60, minimum=0))

    def _max_mood(self) -> float:
        return float(self._get_int("max_mood", 100, minimum=1))

    def _mood_recovery_per_hour(self) -> float:
        return self._get_float("mood_recovery_per_hour", 4.0, minimum=0.0)

    def _unknown_favorability_penalty(self) -> float:
        return self._get_float("unknown_favorability_penalty", 1.0, minimum=0.0)

    def _unknown_mood_penalty(self) -> float:
        return self._get_float("unknown_mood_penalty", 2.0, minimum=0.0)

    def _disliked_favorability_penalty(self) -> float:
        return self._get_float("disliked_favorability_penalty", 2.0, minimum=0.0)

    def _disliked_mood_penalty(self) -> float:
        return self._get_float("disliked_mood_penalty", 6.0, minimum=0.0)

    def _too_full_favorability_penalty(self) -> float:
        return self._get_float("too_full_favorability_penalty", 0.0, minimum=0.0)

    def _too_full_mood_penalty(self) -> float:
        return self._get_float("too_full_mood_penalty", 3.0, minimum=0.0)

    def _llm_enabled(self) -> bool:
        return self._get_bool("llm_enabled", True)

    def _llm_provider_id(self) -> str:
        return self._get_text("llm_provider_id", "")

    def _llm_prompt(self) -> str:
        return self._get_text(
            "llm_prompt",
            (
                "你要扮演一个被用户投喂的虚拟宠物。请根据食物、喜恶、是否成功吃下、当前饱食度，"
                "输出 1 到 2 句自然中文反应。不要解释插件规则，不要输出 JSON，不要使用 Markdown 列表。"
            ),
        )

    def _clamp_satiety(self, value: float) -> float:
        return max(0.0, min(float(value), self._max_satiety()))

    def _clamp_favorability(self, value: float) -> float:
        return max(0.0, min(float(value), self._max_favorability()))

    def _clamp_mood(self, value: float) -> float:
        return max(0.0, min(float(value), self._max_mood()))

    # ---- 食物配置 ----
    def _default_food_items(self) -> list[dict[str, Any]]:
        return [
            {
                "__template_key": "food_item",
                "name": "苹果",
                "aliases": "apple",
                "satiety_gain": 12,
                "favorability_gain": 2,
                "mood_gain": 4,
                "preference": "like",
                "note": "清甜脆口。",
                "accept_hint": "",
                "refuse_hint": "",
            },
            {
                "__template_key": "food_item",
                "name": "小鱼干",
                "aliases": "鱼干\nfish",
                "satiety_gain": 22,
                "favorability_gain": 3,
                "mood_gain": 6,
                "preference": "like",
                "note": "闻起来很香。",
                "accept_hint": "",
                "refuse_hint": "",
            },
            {
                "__template_key": "food_item",
                "name": "饼干",
                "aliases": "cookie\n小饼干",
                "satiety_gain": 10,
                "favorability_gain": 1,
                "mood_gain": 2,
                "preference": "neutral",
                "note": "普通零食。",
                "accept_hint": "",
                "refuse_hint": "",
            },
            {
                "__template_key": "food_item",
                "name": "苦瓜",
                "aliases": "bitter melon",
                "satiety_gain": 8,
                "favorability_gain": 0,
                "mood_gain": 0,
                "preference": "dislike",
                "note": "它一闻就想跑。",
                "accept_hint": "",
                "refuse_hint": "这东西它是真的不想碰。",
            },
        ]

    def _normalize_food_key(self, text: str) -> str:
        return " ".join(str(text or "").strip().lower().split())

    def _load_food_items(self) -> list[FoodItem]:
        payload = self.config.get("food_items", self._default_food_items())
        if not isinstance(payload, list) or not payload:
            payload = self._default_food_items()

        items: list[FoodItem] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            name = str(raw_item.get("name", "") or "").strip()
            if not name:
                continue

            aliases = [self._normalize_food_key(name)]
            aliases.extend(
                self._normalize_food_key(alias)
                for alias in self._parse_text_list(raw_item.get("aliases", ""))
            )

            deduped: list[str] = []
            for alias in aliases:
                if alias and alias not in deduped:
                    deduped.append(alias)

            preference = str(raw_item.get("preference", "neutral") or "").strip().lower()
            if preference not in {"like", "neutral", "dislike"}:
                preference = "neutral"

            items.append(
                FoodItem(
                    name=name,
                    aliases=tuple(deduped),
                    satiety_gain=float(
                        self._coerce_int(raw_item.get("satiety_gain", 0), 0, minimum=0)
                    ),
                    favorability_gain=float(
                        self._coerce_int(raw_item.get("favorability_gain", 0), 0, minimum=0)
                    ),
                    mood_gain=float(
                        self._coerce_int(raw_item.get("mood_gain", 0), 0, minimum=0)
                    ),
                    preference=preference,
                    note=str(raw_item.get("note", "") or "").strip(),
                    accept_hint=str(raw_item.get("accept_hint", "") or "").strip(),
                    refuse_hint=str(raw_item.get("refuse_hint", "") or "").strip(),
                )
            )
        return items

    def _find_food(self, query: str) -> FoodItem | None:
        normalized = self._normalize_food_key(query)
        if not normalized:
            return None
        for item in self._load_food_items():
            if normalized in item.aliases:
                return item
        return None

    def _preference_label(self, preference: str) -> str:
        return {"like": "喜欢", "neutral": "一般", "dislike": "不喜欢"}.get(preference, "一般")

    # ---- 事件辅助 ----
    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            session_id = str(event.get_session_id() or "").strip()
            sender_id = str(event.get_sender_id() or "").strip()
            return bool(session_id and sender_id and session_id == sender_id)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _extract_plain_text(self, event: AstrMessageEvent) -> str:
        plain_parts: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Plain):
                plain_parts.append(component.text)
        merged = "".join(plain_parts).strip()
        return merged or str(event.get_message_str() or "").strip()

    def _extract_sender_name(self, event: AstrMessageEvent) -> str:
        for attr in ("get_sender_name", "get_sender_nickname"):
            method = getattr(event, attr, None)
            if callable(method):
                try:
                    value = method()
                    if value:
                        return str(value)
                except Exception:
                    pass

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for target in (sender, message_obj):
            if target is None:
                continue
            for attr in ("nickname", "name", "sender_name", "user_name"):
                value = getattr(target, attr, None)
                if value:
                    return str(value)

        return str(event.get_sender_id() or "未知用户")

    def _extract_feed_target(self, event: AstrMessageEvent, fallback_arg: str) -> str:
        text = self._extract_plain_text(event)
        for prefix in ("投喂", "喂食"):
            for actual in (prefix, f"/{prefix}", f"／{prefix}"):
                if text.startswith(actual):
                    remainder = text[len(actual) :].strip()
                    if remainder:
                        return remainder
        return str(fallback_arg or "").strip()

    def _session_key(self, event: AstrMessageEvent) -> str:
        session_id = str(event.get_session_id() or event.get_sender_id() or "default").strip()
        return f"{'private' if self._is_private_chat(event) else 'session'}:{session_id}"

    def _command_gate_error(self, event: AstrMessageEvent) -> str | None:
        if not self._enabled():
            return "喂食插件当前已关闭。"
        if self._ignore_self_messages() and event.get_sender_id() == event.get_self_id():
            return "__ignore__"
        if self._is_private_chat(event) and not self._allow_private_chat():
            return "这个插件当前只允许在群聊中使用。"
        return None

    def _feed_permission_error(self, event: AstrMessageEvent) -> str | None:
        sender_id = str(event.get_sender_id() or "").strip()
        blocked = self._blocked_user_ids()
        if sender_id and sender_id in blocked:
            return "你在禁止投喂名单中，当前没有投喂权限。"

        allowed = self._allowed_user_ids()
        if allowed and sender_id not in allowed:
            return "你不在允许投喂用户列表中，当前没有投喂权限。"

        if self._require_admin_to_feed() and not self._is_private_chat(event) and not self._is_admin(event):
            return "当前只允许管理员在群里投喂。"

        return None

    def _reset_gate_error(self, event: AstrMessageEvent) -> str | None:
        if not self._allow_reset_command():
            return "当前未开放喂食重置命令。"
        if self._reset_requires_admin() and not self._is_private_chat(event) and not self._is_admin(event):
            return "只有管理员可以在群里重置当前宠物状态。"
        return None

    # ---- 数据持久化 ----
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _load_data(self) -> dict[str, Any]:
        if not self.data_file.exists():
            return {"version": 2, "sessions": {}}
        try:
            payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"PetFeeder 读取状态失败，将使用空数据继续运行: {exc}")
            return {"version": 2, "sessions": {}}
        if not isinstance(payload, dict):
            return {"version": 2, "sessions": {}}
        payload.setdefault("version", 2)
        payload.setdefault("sessions", {})
        return payload

    def _save_data(self) -> None:
        temp_file = self.data_file.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(self.data_file)

    def _get_session_state(self, session_key: str, now: datetime) -> dict[str, Any]:
        sessions = self._data.setdefault("sessions", {})
        state = sessions.setdefault(
            session_key,
            {
                "satiety": self._initial_satiety(),
                "favorability": self._initial_favorability(),
                "mood": self._initial_mood(),
                "updated_at": now.isoformat(timespec="seconds"),
                "last_food": "",
                "last_feeder_name": "",
                "accepted_count": 0,
                "refused_count": 0,
                "last_reason": "",
                "feeders": {},
            },
        )
        state.setdefault("satiety", self._initial_satiety())
        state.setdefault("favorability", self._initial_favorability())
        state.setdefault("mood", self._initial_mood())
        state.setdefault("updated_at", now.isoformat(timespec="seconds"))
        state.setdefault("last_food", "")
        state.setdefault("last_feeder_name", "")
        state.setdefault("accepted_count", 0)
        state.setdefault("refused_count", 0)
        state.setdefault("last_reason", "")
        state.setdefault("feeders", {})
        state["satiety"] = self._clamp_satiety(float(state.get("satiety", self._initial_satiety())))
        state["favorability"] = self._clamp_favorability(
            float(state.get("favorability", self._initial_favorability()))
        )
        state["mood"] = self._clamp_mood(float(state.get("mood", self._initial_mood())))
        return state

    def _get_feeder_state(self, state: dict[str, Any], user_id: str, sender_name: str) -> dict[str, Any]:
        feeders = state.setdefault("feeders", {})
        feeder = feeders.setdefault(
            user_id or "unknown",
            {
                "name": sender_name,
                "last_feed_at": "",
                "last_food": "",
                "accepted_count": 0,
                "refused_count": 0,
            },
        )
        feeder.setdefault("name", sender_name)
        feeder.setdefault("last_feed_at", "")
        feeder.setdefault("last_food", "")
        feeder.setdefault("accepted_count", 0)
        feeder.setdefault("refused_count", 0)
        feeder["name"] = sender_name or feeder.get("name", "")
        return feeder

    def _find_feeder_state(self, state: dict[str, Any], user_id: str) -> dict[str, Any] | None:
        feeders = state.get("feeders", {})
        if not isinstance(feeders, dict):
            return None
        feeder = feeders.get(user_id)
        return feeder if isinstance(feeder, dict) else None

    def _drift_toward(self, value: float, target: float, amount: float) -> float:
        if amount <= 0:
            return value
        if value < target:
            return min(target, value + amount)
        if value > target:
            return max(target, value - amount)
        return value

    def _apply_decay(self, state: dict[str, Any], now: datetime) -> None:
        satiety = self._clamp_satiety(float(state.get("satiety", self._initial_satiety())))
        favorability = self._clamp_favorability(
            float(state.get("favorability", self._initial_favorability()))
        )
        mood = self._clamp_mood(float(state.get("mood", self._initial_mood())))
        previous = self._parse_iso_datetime(str(state.get("updated_at", "") or ""))
        if previous is not None:
            elapsed_hours = max(0.0, (now - previous).total_seconds() / 3600.0)
            if self._satiety_decay_per_hour() > 0:
                satiety -= elapsed_hours * self._satiety_decay_per_hour()
            if self._mood_recovery_per_hour() > 0:
                mood = self._drift_toward(
                    mood,
                    self._initial_mood(),
                    elapsed_hours * self._mood_recovery_per_hour(),
                )
        state["satiety"] = self._clamp_satiety(satiety)
        state["favorability"] = self._clamp_favorability(favorability)
        state["mood"] = self._clamp_mood(mood)
        state["updated_at"] = now.isoformat(timespec="seconds")

    def _cooldown_remaining_seconds(self, feeder: dict[str, Any] | None, now: datetime) -> float:
        if feeder is None or self._feed_cooldown_seconds() <= 0:
            return 0.0
        previous = self._parse_iso_datetime(str(feeder.get("last_feed_at", "") or ""))
        if previous is None:
            return 0.0
        elapsed_seconds = max(0.0, (now - previous).total_seconds())
        return max(0.0, self._feed_cooldown_seconds() - elapsed_seconds)

    # ---- 输出文本 ----
    def _satiety_label(self, satiety: float) -> str:
        maximum = self._max_satiety()
        ratio = 0.0 if maximum <= 0 else satiety / maximum
        refuse_ratio = 0.0 if maximum <= 0 else self._refuse_threshold() / maximum
        if ratio <= 0.15:
            return "饿坏了"
        if ratio <= 0.4:
            return "有点饿"
        if ratio <= 0.75:
            return "状态正好"
        if ratio < refuse_ratio:
            return "已经很满足"
        return "快要吃撑了"

    def _favorability_label(self, favorability: float) -> str:
        maximum = self._max_favorability()
        ratio = 0.0 if maximum <= 0 else favorability / maximum
        if ratio < 0.2:
            return "非常疏远"
        if ratio < 0.45:
            return "有点防备"
        if ratio < 0.7:
            return "开始亲近"
        if ratio < 0.9:
            return "很喜欢你"
        return "几乎离不开你"

    def _mood_label(self, mood: float) -> str:
        maximum = self._max_mood()
        ratio = 0.0 if maximum <= 0 else mood / maximum
        if ratio < 0.2:
            return "心情很差"
        if ratio < 0.45:
            return "不太高兴"
        if ratio < 0.7:
            return "还算平稳"
        if ratio < 0.9:
            return "心情不错"
        return "非常开心"

    def _build_status_text(
        self,
        state: dict[str, Any],
        cooldown_remaining: float = 0.0,
    ) -> str:
        satiety = self._clamp_satiety(float(state.get("satiety", 0.0)))
        favorability = self._clamp_favorability(
            float(state.get("favorability", self._initial_favorability()))
        )
        mood = self._clamp_mood(float(state.get("mood", self._initial_mood())))
        last_food = str(state.get("last_food", "") or "").strip() or "暂无"
        last_feeder = str(state.get("last_feeder_name", "") or "").strip() or "暂无"

        lines = [
            f"{self._pet_name()} 当前状态",
            f"饱食度：{self._format_number(satiety)}/{self._format_number(self._max_satiety())}",
            f"饱腹状态：{self._satiety_label(satiety)}",
            f"好感度：{self._format_number(favorability)}/{self._format_number(self._max_favorability())}（{self._favorability_label(favorability)}）",
            f"心情值：{self._format_number(mood)}/{self._format_number(self._max_mood())}（{self._mood_label(mood)}）",
            f"上次吃的食物：{last_food}",
            f"上次投喂人：{last_feeder}",
            f"成功进食次数：{int(state.get('accepted_count', 0))}",
            f"拒绝进食次数：{int(state.get('refused_count', 0))}",
        ]

        if self._feed_cooldown_seconds() > 0:
            if cooldown_remaining > 0:
                lines.append(f"你的投喂冷却：剩余 {int(cooldown_remaining + 0.999)} 秒")
            else:
                lines.append("你的投喂冷却：已就绪")

        return "\n".join(lines)

    def _build_food_catalog(self) -> str:
        items = self._load_food_items()
        if not items:
            return "当前没有配置任何可投喂食物。"

        lines = ["可投喂食物："]
        for item in items:
            lines.append(
                f"- {item.name} | 饱食度 +{self._format_number(item.satiety_gain)} | "
                f"好感 +{self._format_number(item.favorability_gain)} | "
                f"心情 +{self._format_number(item.mood_gain)} | "
                f"{self._preference_label(item.preference)}"
            )
        return "\n".join(lines)

    def _build_permission_summary(self) -> str:
        allowed = self._allowed_user_ids()
        blocked = self._blocked_user_ids()
        allowed_text = str(len(allowed)) if allowed else "全部用户"
        blocked_text = str(len(blocked)) if blocked else "0"
        admin_text = "是" if self._require_admin_to_feed() else "否"
        return (
            f"管理员限定投喂：{admin_text}\n"
            f"允许投喂用户：{allowed_text}\n"
            f"禁止投喂用户数：{blocked_text}\n"
            f"投喂冷却：{self._format_number(self._feed_cooldown_seconds())} 秒"
        )

    def _build_help_text(self) -> str:
        return (
            "喂食插件命令\n"
            "1. 投喂 食物名\n"
            "2. 喂食 食物名\n"
            "3. 喂食状态\n"
            "4. 饱食度\n"
            "5. 好感度\n"
            "6. 心情值\n"
            "7. 喂食帮助\n"
            "8. 喂食重置\n\n"
            f"当前宠物：{self._pet_name()}\n"
            f"初始饱食度：{self._format_number(self._initial_satiety())}\n"
            f"初始好感度：{self._format_number(self._initial_favorability())}\n"
            f"初始心情值：{self._format_number(self._initial_mood())}\n"
            f"拒食阈值：{self._format_number(self._refuse_threshold())}\n"
            f"每小时饱食度衰减：{self._format_number(self._satiety_decay_per_hour())}\n"
            f"每小时心情回稳：{self._format_number(self._mood_recovery_per_hour())}\n\n"
            f"{self._build_permission_summary()}\n\n"
            f"{self._build_food_catalog()}"
        )

    def _fallback_reaction(
        self,
        food_query: str,
        food: FoodItem | None,
        outcome: str,
        satiety_before: float,
        satiety_after: float,
        favorability_before: float,
        favorability_after: float,
        mood_before: float,
        mood_after: float,
    ) -> str:
        pet_name = self._pet_name()
        food_name = food.name if food else food_query
        favorability_delta = favorability_after - favorability_before
        mood_delta = mood_after - mood_before

        if outcome == OUTCOME_ACCEPTED and food is not None:
            reaction = (
                f"{pet_name}眼睛一下亮了，开心地把{food_name}吃掉了。"
                if food.preference == "like"
                else f"{pet_name}认真地吃掉了{food_name}，看起来还算满意。"
            )
            if food.accept_hint:
                reaction = f"{reaction}\n{food.accept_hint}"
            return (
                f"{reaction}\n"
                f"饱食度 +{self._format_number(food.satiety_gain)}，"
                f"好感 +{self._format_number(favorability_delta)}，"
                f"心情 +{self._format_number(mood_delta)}。\n"
                f"当前状态：饱食度 {self._format_number(satiety_after)}/{self._format_number(self._max_satiety())}，"
                f"好感度 {self._format_number(favorability_after)}/{self._format_number(self._max_favorability())}，"
                f"心情值 {self._format_number(mood_after)}/{self._format_number(self._max_mood())}。"
            )

        if outcome == OUTCOME_TOO_FULL:
            extra = f"\n{food.refuse_hint}" if food and food.refuse_hint else ""
            return (
                f"{pet_name}已经吃得肚子圆滚滚了，摇着脑袋拒绝再吃{food_name}。{extra}\n"
                f"本次心情 {self._format_number(abs(mood_delta))} 点波动，当前心情值 "
                f"{self._format_number(mood_after)}/{self._format_number(self._max_mood())}。"
            )

        if outcome == OUTCOME_DISLIKED:
            extra = f"\n{food.refuse_hint}" if food and food.refuse_hint else ""
            return (
                f"{pet_name}闻了闻{food_name}，立刻把头扭开了，完全不想吃。{extra}\n"
                f"{food_name} 被标记为“不喜欢”，本次没有增加饱食度，"
                f"好感 {self._format_number(abs(favorability_delta))} 点、心情 {self._format_number(abs(mood_delta))} 点发生了下滑。"
            )

        return (
            f"{pet_name}盯着“{food_query}”看了半天，像是在说自己没见过这种食物。\n"
            f"它的心情略微受到了影响，当前好感度 {self._format_number(favorability_after)}/{self._format_number(self._max_favorability())}，"
            f"心情值 {self._format_number(mood_after)}/{self._format_number(self._max_mood())}。\n"
            "请先在插件配置里添加这类食物，或使用“喂食帮助”查看当前可投喂列表。"
        )

    async def _build_reaction(
        self,
        food_query: str,
        food: FoodItem | None,
        outcome: str,
        satiety_before: float,
        satiety_after: float,
        favorability_before: float,
        favorability_after: float,
        mood_before: float,
        mood_after: float,
    ) -> str:
        fallback = self._fallback_reaction(
            food_query,
            food,
            outcome,
            satiety_before,
            satiety_after,
            favorability_before,
            favorability_after,
            mood_before,
            mood_after,
        )
        if not self._llm_enabled():
            return fallback

        provider = None
        provider_id = self._llm_provider_id()
        try:
            provider = self.context.get_provider_by_id(provider_id) if provider_id else self.context.get_using_provider()
            if provider is None and provider_id:
                provider = self.context.get_using_provider()
        except Exception:
            provider = None

        if not isinstance(provider, Provider):
            return fallback

        food_name = food.name if food else food_query
        prompt = "\n".join(
            [
                f"宠物名字：{self._pet_name()}",
                f"动作结果：{outcome}",
                f"食物：{food_name}",
                f"食物偏好：{self._preference_label(food.preference) if food else '未知'}",
                f"食物备注：{food.note if food else ''}",
                f"进食前饱食度：{self._format_number(satiety_before)} / {self._format_number(self._max_satiety())}",
                f"进食后饱食度：{self._format_number(satiety_after)} / {self._format_number(self._max_satiety())}",
                f"进食前好感度：{self._format_number(favorability_before)} / {self._format_number(self._max_favorability())}",
                f"进食后好感度：{self._format_number(favorability_after)} / {self._format_number(self._max_favorability())}",
                f"进食前心情值：{self._format_number(mood_before)} / {self._format_number(self._max_mood())}",
                f"进食后心情值：{self._format_number(mood_after)} / {self._format_number(self._max_mood())}",
                f"拒食阈值：{self._format_number(self._refuse_threshold())}",
                "请直接输出宠物当场的说话或反应，不要解释系统规则。",
            ]
        )
        try:
            response = await provider.text_chat(system_prompt=self._llm_prompt(), prompt=prompt)
            text = str(getattr(response, "completion_text", "") or "").strip()
            return text or fallback
        except Exception as exc:
            logger.warning(f"PetFeeder 调用 LLM 生成反应失败，已回退模板文案: {exc}")
            return fallback

    # ---- 命令 ----
    @filter.command("投喂")
    async def feed(
        self, event: AstrMessageEvent, food_name: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        event.should_call_llm(False)
        gate_error = self._command_gate_error(event)
        if gate_error == "__ignore__":
            return
        if gate_error:
            yield event.plain_result(gate_error)
            return

        permission_error = self._feed_permission_error(event)
        if permission_error:
            yield event.plain_result(permission_error)
            return

        target_food = self._extract_feed_target(event, food_name)
        if not target_food:
            yield event.plain_result("请在命令后面带上食物名，例如：投喂 苹果")
            return

        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = self._extract_sender_name(event)
        outcome = OUTCOME_UNKNOWN
        matched_food: FoodItem | None = None
        satiety_before = 0.0
        satiety_after = 0.0
        favorability_before = 0.0
        favorability_after = 0.0
        mood_before = 0.0
        mood_after = 0.0
        cooldown_message = ""

        async with self._lock:
            now = self._now()
            state = self._get_session_state(self._session_key(event), now)
            self._apply_decay(state, now)
            feeder = self._get_feeder_state(state, sender_id, sender_name)
            cooldown_remaining = self._cooldown_remaining_seconds(feeder, now)

            if cooldown_remaining > 0:
                self._save_data()
                cooldown_message = (
                    f"你刚喂过{self._pet_name()}，请 {int(cooldown_remaining + 0.999)} 秒后再试。\n"
                    f"{self._build_status_text(state, cooldown_remaining)}"
                )
            else:
                satiety_before = self._clamp_satiety(float(state.get("satiety", 0.0)))
                favorability_before = self._clamp_favorability(
                    float(state.get("favorability", self._initial_favorability()))
                )
                mood_before = self._clamp_mood(float(state.get("mood", self._initial_mood())))
                matched_food = self._find_food(target_food)

                if matched_food is None:
                    state["refused_count"] = int(state.get("refused_count", 0)) + 1
                    state["favorability"] = self._clamp_favorability(
                        favorability_before - self._unknown_favorability_penalty()
                    )
                    state["mood"] = self._clamp_mood(mood_before - self._unknown_mood_penalty())
                    state["last_reason"] = OUTCOME_UNKNOWN
                    satiety_after = satiety_before
                elif matched_food.preference == "dislike":
                    outcome = OUTCOME_DISLIKED
                    state["refused_count"] = int(state.get("refused_count", 0)) + 1
                    state["favorability"] = self._clamp_favorability(
                        favorability_before - self._disliked_favorability_penalty()
                    )
                    state["mood"] = self._clamp_mood(mood_before - self._disliked_mood_penalty())
                    state["last_reason"] = OUTCOME_DISLIKED
                    satiety_after = satiety_before
                elif satiety_before >= self._refuse_threshold():
                    outcome = OUTCOME_TOO_FULL
                    state["refused_count"] = int(state.get("refused_count", 0)) + 1
                    state["favorability"] = self._clamp_favorability(
                        favorability_before - self._too_full_favorability_penalty()
                    )
                    state["mood"] = self._clamp_mood(mood_before - self._too_full_mood_penalty())
                    state["last_reason"] = OUTCOME_TOO_FULL
                    satiety_after = satiety_before
                else:
                    outcome = OUTCOME_ACCEPTED
                    satiety_after = self._clamp_satiety(satiety_before + matched_food.satiety_gain)
                    state["satiety"] = satiety_after
                    state["favorability"] = self._clamp_favorability(
                        favorability_before + matched_food.favorability_gain
                    )
                    state["mood"] = self._clamp_mood(mood_before + matched_food.mood_gain)
                    state["last_food"] = matched_food.name
                    state["accepted_count"] = int(state.get("accepted_count", 0)) + 1
                    state["last_reason"] = OUTCOME_ACCEPTED

                state["last_feeder_name"] = sender_name
                state["updated_at"] = now.isoformat(timespec="seconds")
                favorability_after = self._clamp_favorability(
                    float(state.get("favorability", self._initial_favorability()))
                )
                mood_after = self._clamp_mood(float(state.get("mood", self._initial_mood())))

                feeder["last_feed_at"] = now.isoformat(timespec="seconds")
                feeder["last_food"] = target_food
                if outcome == OUTCOME_ACCEPTED:
                    feeder["accepted_count"] = int(feeder.get("accepted_count", 0)) + 1
                else:
                    feeder["refused_count"] = int(feeder.get("refused_count", 0)) + 1

                self._save_data()

        if cooldown_message:
            yield event.plain_result(cooldown_message)
            return

        reaction = await self._build_reaction(
            target_food,
            matched_food,
            outcome,
            satiety_before,
            satiety_after,
            favorability_before,
            favorability_after,
            mood_before,
            mood_after,
        )
        yield event.plain_result(reaction)

    @filter.command("喂食")
    async def feed_alias(
        self, event: AstrMessageEvent, food_name: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self.feed(event, food_name):
            yield result

    @filter.command("喂食状态")
    async def status(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        event.should_call_llm(False)
        gate_error = self._command_gate_error(event)
        if gate_error == "__ignore__":
            return
        if gate_error:
            yield event.plain_result(gate_error)
            return

        async with self._lock:
            now = self._now()
            state = self._get_session_state(self._session_key(event), now)
            self._apply_decay(state, now)
            feeder = self._find_feeder_state(state, str(event.get_sender_id() or "").strip())
            cooldown_remaining = self._cooldown_remaining_seconds(feeder, now)
            self._save_data()
            message = self._build_status_text(state, cooldown_remaining)
        yield event.plain_result(message)

    @filter.command("饱食度")
    async def status_alias(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self.status(event):
            yield result

    @filter.command("好感度")
    async def favorability(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self.status(event):
            yield result

    @filter.command("心情")
    async def mood(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self.status(event):
            yield result

    @filter.command("心情值")
    async def mood_alias(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self.status(event):
            yield result

    @filter.command("喂食帮助")
    async def help(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        event.should_call_llm(False)
        gate_error = self._command_gate_error(event)
        if gate_error == "__ignore__":
            return
        if gate_error:
            yield event.plain_result(gate_error)
            return
        yield event.plain_result(self._build_help_text())

    @filter.command("喂食重置")
    async def reset(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        event.should_call_llm(False)
        gate_error = self._command_gate_error(event)
        if gate_error == "__ignore__":
            return
        if gate_error:
            yield event.plain_result(gate_error)
            return

        reset_error = self._reset_gate_error(event)
        if reset_error:
            yield event.plain_result(reset_error)
            return

        async with self._lock:
            now = self._now()
            state = self._get_session_state(self._session_key(event), now)
            state["satiety"] = self._initial_satiety()
            state["favorability"] = self._initial_favorability()
            state["mood"] = self._initial_mood()
            state["last_food"] = ""
            state["last_feeder_name"] = ""
            state["accepted_count"] = 0
            state["refused_count"] = 0
            state["last_reason"] = "reset"
            state["feeders"] = {}
            state["updated_at"] = now.isoformat(timespec="seconds")
            self._save_data()

        yield event.plain_result(
            f"{self._pet_name()} 的状态已重置为初始值："
            f"饱食度 {self._format_number(self._initial_satiety())}，"
            f"好感度 {self._format_number(self._initial_favorability())}，"
            f"心情值 {self._format_number(self._initial_mood())}。"
        )
