"""跨审计复核标记：冲突指纹计算、失效时间解析与标记附着。

复核标记不绑定单次审计，而是绑定 *冲突指纹* —— 由冲突/告警条目的
**祖先节点、被依赖节点与双方规范化许可证** 共同决定。因此：

* 同一份 manifest 无论审计多少次，只要四元组不变，标记就会跟随（跨审计生效）；
* 任一方许可证发生变化 → 指纹改变 → 旧标记不再附着，条目自动回到「待复核」；
* 标记过期或被撤销 → 不再附着，条目同样自动回到「待复核」。

附着只发生在 API 读取层（``attach_reviews``），数据库中保存的审计结果
保持原始形态，两次审计对比（``compare``）因此完全不受复核标记影响。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import database, licenses

# 条目复核状态（附着在冲突/告警条目上）
STATE_PENDING = "pending"
STATE_REVIEWED = "reviewed"

# 复核记录的生命周期状态（effective_state）
MARK_ACTIVE = "active"
MARK_EXPIRED = "expired"
MARK_REVOKED = "revoked"

_FINGERPRINT_SEP = "\x1f"


def conflict_fingerprint(
    ancestor: object,
    node: object,
    ancestor_license: object,
    node_license: object,
) -> str:
    """对（祖先节点, 被依赖节点, 双方规范化许可证）计算稳定指纹。

    许可证一律先规范化为矩阵使用的 token 再参与计算，因此
    ``Apache 2.0`` 与 ``Apache-2.0`` 这类写法差异不会产生新指纹。
    """
    ancestor_token = licenses.normalize_license(ancestor_license)[1]
    node_token = licenses.normalize_license(node_license)[1]
    raw = _FINGERPRINT_SEP.join(
        [
            str(ancestor).strip().lower(),
            str(node).strip().lower(),
            ancestor_token,
            node_token,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def fingerprint_of(conflict: Dict[str, Any]) -> str:
    """从审计结果中的冲突/告警条目直接计算指纹。"""
    return conflict_fingerprint(
        conflict.get("ancestor"),
        conflict.get("node"),
        conflict.get("ancestor_license"),
        conflict.get("node_license"),
    )


def parse_expires_at(raw: object) -> Optional[str]:
    """把用户输入的失效时间规范化为 UTC ISO 字符串；空值表示永久有效。

    接受 ISO 8601（可带 ``Z`` 或时区偏移）；不带时区时按 UTC 处理。
    格式非法时抛出 ``ValueError``（由 API 层转成 422）。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "失效时间格式无法解析：%r，请使用 ISO 8601（如 2026-12-31T23:59:59Z）。" % text
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def is_expired(mark: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    expires_at = mark.get("expires_at")
    if not expires_at:
        return False
    now = now or datetime.now(timezone.utc)
    return _parse_dt(expires_at) <= now


def effective_state(mark: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """复核记录的当前有效状态：active / expired / revoked。"""
    if mark.get("status") == MARK_REVOKED:
        return MARK_REVOKED
    if is_expired(mark, now):
        return MARK_EXPIRED
    return MARK_ACTIVE


def public_mark(mark: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """复核记录的对外表示：附带计算出的 effective_state。"""
    return dict(mark, effective_state=effective_state(mark, now))


def attach_reviews(
    result: Dict[str, Any],
    marks: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """把最新有效复核标记附到审计结果的每条冲突/告警上（就地修改并返回）。

    仅「指纹一致 + 未撤销 + 未过期」的最新标记会被附上，条目状态为
    ``reviewed``；其余条目状态为 ``pending``。冲突本身的内容、严重程度
    与最短路径均不改动，只新增 ``review`` 字段与 ``summary.review`` 统计。
    """
    if marks is None:
        marks = database.active_review_marks(now=now)
    conflicts = result.get("conflicts", [])
    reviewed = 0
    for conflict in conflicts:
        mark = marks.get(fingerprint_of(conflict))
        if mark is None:
            conflict["review"] = {"state": STATE_PENDING}
        else:
            reviewed += 1
            conflict["review"] = {
                "state": STATE_REVIEWED,
                "mark_id": mark["id"],
                "note": mark["note"],
                "expires_at": mark["expires_at"],
                "marked_at": mark["created_at"],
            }
    summary = result.setdefault("summary", {})
    summary["review"] = {
        "total": len(conflicts),
        "reviewed": reviewed,
        "pending": len(conflicts) - reviewed,
    }
    return result
