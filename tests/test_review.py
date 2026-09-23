"""跨审计复核标记的自测（无测试框架依赖）：python -m tests.test_review

使用临时 SQLite 数据库，不污染开发数据。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import analyzer, compare as compare_mod, database, review

# 在 init_db 之前把数据库指向临时目录
_TMP_DIR = Path(tempfile.mkdtemp(prefix="review-test-"))
database.DB_PATH = _TMP_DIR / "audit.db"
database.init_db()


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError("%s %s" % (name, detail))
    print("ok -", name)


def _manifest(node_license: str = "Proprietary") -> dict:
    return {
        "name": "review-demo",
        "packages": [
            {"name": "app", "version": "1", "license": "GPL-3.0",
             "dependencies": ["lib@1"]},
            {"name": "lib", "version": "1", "license": node_license},
        ],
    }


def _only_conflict(result: dict) -> dict:
    assert len(result["conflicts"]) == 1, result["conflicts"]
    return result["conflicts"][0]


def _mark_conflict(conflict: dict, note: str = "已确认", expires_at=None) -> dict:
    return database.save_review_mark(
        fingerprint=review.fingerprint_of(conflict),
        ancestor=conflict["ancestor"],
        node=conflict["node"],
        ancestor_license="GPL-3.0",
        node_license="Proprietary",
        note=note,
        expires_at=expires_at,
    )


def test_fingerprint_normalizes_licenses() -> None:
    fp1 = review.conflict_fingerprint("app@1", "lib@1", "Apache 2.0", "mit license")
    fp2 = review.conflict_fingerprint("app@1", "lib@1", "Apache-2.0", "MIT")
    _check("display text differences share fingerprint", fp1 == fp2)
    fp3 = review.conflict_fingerprint("app@1", "lib@1", "Apache-2.0", "GPL-3.0")
    _check("license change changes fingerprint", fp3 != fp1)
    fp4 = review.conflict_fingerprint("app@1", "other@1", "Apache-2.0", "MIT")
    _check("node change changes fingerprint", fp4 != fp1)


def test_parse_expires_at() -> None:
    _check("empty means never expires", review.parse_expires_at(None) is None)
    _check("blank means never expires", review.parse_expires_at("  ") is None)
    parsed = review.parse_expires_at("2026-12-31T23:59:59Z")
    _check("Z suffix parsed", parsed is not None and parsed.startswith("2026-12-31T23:59:59"), str(parsed))
    naive = review.parse_expires_at("2026-12-31T10:00:00")
    _check("naive treated as UTC", naive is not None and naive.endswith("+00:00"), str(naive))
    try:
        review.parse_expires_at("not-a-date")
    except ValueError:
        print("ok - invalid expires_at raises ValueError")
    else:
        raise AssertionError("invalid expires_at should raise ValueError")


def test_attach_and_cross_audit() -> None:
    first = analyzer.analyze_manifest(_manifest())
    conflict = _only_conflict(first)
    mark = _mark_conflict(conflict, note="法务已确认组合分发")

    # 第一次审计读取：标记附着，状态为已复核
    result1 = analyzer.analyze_manifest(_manifest())
    review.attach_reviews(result1)
    attached = _only_conflict(result1)["review"]
    _check("conflict marked reviewed", attached["state"] == review.STATE_REVIEWED)
    _check("note attached", attached["note"] == "法务已确认组合分发")
    _check("mark id attached", attached["mark_id"] == mark["id"])
    summary = result1["summary"]["review"]
    _check("summary counts", summary == {"total": 1, "reviewed": 1, "pending": 0}, str(summary))

    # 跨审计：重新审计同一 manifest（新的 audit），标记仍然生效
    result2 = analyzer.analyze_manifest(_manifest())
    review.attach_reviews(result2)
    _check(
        "mark follows across audits",
        _only_conflict(result2)["review"]["state"] == review.STATE_REVIEWED,
    )

    # 原始冲突内容不被改动（仅新增 review 字段）
    original = _only_conflict(analyzer.analyze_manifest(_manifest()))
    attached_conflict = _only_conflict(result2)
    for key in ("severity", "status", "ancestor", "node", "distance", "path", "reason"):
        _check("conflict %s untouched" % key, attached_conflict[key] == original[key])


def test_license_change_returns_to_pending() -> None:
    # 已标记 GPL-3.0 -> Proprietary；把依赖许可证换成 AGPL-3.0 后仍冲突但指纹变化
    result = analyzer.analyze_manifest(_manifest(node_license="AGPL-3.0"))
    review.attach_reviews(result)
    conflict = _only_conflict(result)
    _check("license change keeps conflict detected", conflict["node_license"] == "AGPL-3.0")
    _check(
        "license change back to pending",
        conflict["review"]["state"] == review.STATE_PENDING,
    )
    _check("pending counted", result["summary"]["review"]["pending"] == 1)


def test_expired_and_revoked_return_to_pending() -> None:
    conflict = _only_conflict(analyzer.analyze_manifest(_manifest(node_license="AGPL-3.0")))

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    expired_mark = database.save_review_mark(
        fingerprint=review.fingerprint_of(conflict),
        ancestor=conflict["ancestor"],
        node=conflict["node"],
        ancestor_license="GPL-3.0",
        node_license="AGPL-3.0",
        note="过期标记",
        expires_at=past,
    )
    _check("expired mark reported expired", review.effective_state(expired_mark) == review.MARK_EXPIRED)
    result = analyzer.analyze_manifest(_manifest(node_license="AGPL-3.0"))
    review.attach_reviews(result)
    _check(
        "expired mark not attached",
        _only_conflict(result)["review"]["state"] == review.STATE_PENDING,
    )

    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    mark = database.save_review_mark(
        fingerprint=review.fingerprint_of(conflict),
        ancestor=conflict["ancestor"],
        node=conflict["node"],
        ancestor_license="GPL-3.0",
        node_license="AGPL-3.0",
        note="有效标记",
        expires_at=future,
    )
    result = analyzer.analyze_manifest(_manifest(node_license="AGPL-3.0"))
    review.attach_reviews(result)
    _check(
        "valid mark attached",
        _only_conflict(result)["review"]["state"] == review.STATE_REVIEWED,
    )

    _check("revoke succeeds", database.revoke_review_mark(mark["id"]) is True)
    _check("double revoke fails", database.revoke_review_mark(mark["id"]) is False)
    revoked = database.get_review_mark(mark["id"])
    _check("revoked state", revoked is not None and review.effective_state(revoked) == review.MARK_REVOKED)
    _check("revoked_at recorded", bool(revoked and revoked["revoked_at"]))
    result = analyzer.analyze_manifest(_manifest(node_license="AGPL-3.0"))
    review.attach_reviews(result)
    _check(
        "revoked mark back to pending",
        _only_conflict(result)["review"]["state"] == review.STATE_PENDING,
    )


def test_latest_valid_mark_wins() -> None:
    conflict = _only_conflict(analyzer.analyze_manifest(_manifest(node_license="GPL-2.0")))
    fp = review.fingerprint_of(conflict)
    older = database.save_review_mark(fp, conflict["ancestor"], conflict["node"],
                                      "GPL-3.0", "GPL-2.0", "旧备注", None)
    newer = database.save_review_mark(fp, conflict["ancestor"], conflict["node"],
                                      "GPL-3.0", "GPL-2.0", "新备注", None)
    result = analyzer.analyze_manifest(_manifest(node_license="GPL-2.0"))
    review.attach_reviews(result)
    attached = _only_conflict(result)["review"]
    _check("latest mark wins", attached["mark_id"] == newer["id"] and attached["note"] == "新备注")

    # 撤销最新标记后，仍有效的旧标记顶上（它未过期未撤销）
    database.revoke_review_mark(newer["id"])
    result = analyzer.analyze_manifest(_manifest(node_license="GPL-2.0"))
    review.attach_reviews(result)
    attached = _only_conflict(result)["review"]
    _check("older still-valid mark takes over", attached["mark_id"] == older["id"], str(attached))
    database.revoke_review_mark(older["id"])


def test_compare_unaffected() -> None:
    base = analyzer.analyze_manifest(_manifest())
    base["audit_id"] = 101
    target = analyzer.analyze_manifest(_manifest(node_license="MIT"))
    target["audit_id"] = 102
    diff = compare_mod.compare_audits(base, target)
    _check("compare still read only", diff["read_only"] is True)
    findings = diff["added"] + diff["removed"] + diff["changed"]
    _check("compare sees the license-driven change", len(findings) >= 1)
    for finding in findings:
        for side in (finding["base"], finding["target"]):
            if side is not None:
                _check("no review field leaks into compare", "review" not in side)


def test_records_listing() -> None:
    items = database.list_review_marks()
    _check("records persisted", len(items) >= 4, str(len(items)))
    states = {review.effective_state(m) for m in items}
    _check("revoked/expired kept in records", review.MARK_REVOKED in states and review.MARK_EXPIRED in states, str(states))
    public = review.public_mark(items[0])
    _check("public mark carries effective_state", "effective_state" in public)


def main() -> None:
    test_fingerprint_normalizes_licenses()
    test_parse_expires_at()
    test_attach_and_cross_audit()
    test_license_change_returns_to_pending()
    test_expired_and_revoked_return_to_pending()
    test_latest_valid_mark_wins()
    test_compare_unaffected()
    test_records_listing()
    print("\nall review tests passed.")


if __name__ == "__main__":
    main()
