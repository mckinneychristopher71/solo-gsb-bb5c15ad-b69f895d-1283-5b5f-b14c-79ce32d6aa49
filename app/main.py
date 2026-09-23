"""FastAPI 入口：JSON API + 简洁网页。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyzer, compare as compare_mod, database, licenses, review as review_mod

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SAMPLE_PATH = STATIC_DIR / "examples" / "sample-manifest.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化 SQLite（建库建表）
    database.init_db()
    yield


app = FastAPI(
    title="许可证依赖冲突审计台",
    description=(
        "上传包含包名、版本、许可证与依赖边的 manifest，"
        "系统规范化节点后按许可证兼容矩阵计算传递约束，"
        "标出每个冲突的最短依赖路径，支持按包筛选与 JSON 导出。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


class AuditRequest(BaseModel):
    manifest: Dict[str, Any] = Field(..., description="待审计的 manifest 对象")
    name: Optional[str] = Field(None, description="本次审计的名称（可选）")


class ReviewMarkRequest(BaseModel):
    audit_id: int = Field(..., description="产生该冲突/告警的审计 ID")
    conflict_id: str = Field(..., description="审计结果中冲突/告警条目的 id（如 C001）")
    note: Optional[str] = Field(None, description="复核备注（可选）")
    expires_at: Optional[str] = Field(
        None, description="标记失效时间（ISO 8601，如 2026-12-31T23:59:59Z）；留空表示永久有效"
    )


def _audit_payload(manifest: Any, name: Optional[str]) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=422, detail="manifest 必须是 JSON 对象。")
    result = analyzer.analyze_manifest(manifest, name or "uploaded-manifest")
    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("error", "manifest 无法解析。"))
    audit_id = database.save_audit(result, manifest)
    result["audit_id"] = audit_id
    # 复核标记在读取层附着，不写入持久化的审计结果
    return review_mod.attach_reviews(result)


# --- JSON API ---------------------------------------------------------------


@app.post("/api/audit", tags=["audit"], summary="审计 manifest（JSON 请求体）")
def audit_manifest(payload: AuditRequest) -> Dict[str, Any]:
    manifest = dict(payload.manifest)
    if payload.name:
        manifest.setdefault("name", payload.name)
    return _audit_payload(manifest, payload.name)


@app.post("/api/audit/upload", tags=["audit"], summary="上传 manifest 文件审计")
async def upload_manifest(
    file: UploadFile = File(..., description="manifest JSON 文件"),
    name: Optional[str] = None,
) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="上传文件为空。")
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="无法解析 JSON 文件：%s" % exc)
    default_name = name or Path(file.filename or "uploaded-manifest").stem
    return _audit_payload(manifest, default_name)


@app.get("/api/audits", tags=["audit"], summary="历史审计列表")
def list_audits(limit: int = 20) -> Dict[str, Any]:
    limit = max(1, min(limit, 100))
    return {"items": database.list_audits(limit)}


@app.get(
    "/api/audits/compare/{base_id}/{target_id}",
    tags=["audit"],
    summary="对比两次审计（按包名+版本配对节点与冲突，纯只读）",
)
def compare_audits(base_id: int, target_id: int) -> Dict[str, Any]:
    if base_id == target_id:
        raise HTTPException(status_code=400, detail="请选择两条不同的审计记录进行对比：%d 与 %d 相同。" % (base_id, target_id))
    missing = [
        aid for aid in (base_id, target_id)
        if not database.audit_exists(aid)
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail="审计记录不存在：%s。对比为只读操作，不会创建新审计。"
            % "、".join("#%d" % aid for aid in missing),
        )
    base_result = database.get_audit(base_id)
    target_result = database.get_audit(target_id)
    return compare_mod.compare_audits(base_result, target_result)


@app.get("/api/audits/{audit_id}", tags=["audit"], summary="获取某次审计结果（支持按包筛选/导出）")
def get_audit(
    audit_id: int,
    package: Optional[str] = None,
    download: bool = False,
) -> Any:
    result = database.get_audit(audit_id)
    if result is None:
        raise HTTPException(status_code=404, detail="审计记录不存在：%s" % audit_id)
    result = analyzer.filter_result(result, package)
    # 读取任意历史审计时，仅附上指纹一致且未过期/未撤销的最新复核标记
    result = review_mod.attach_reviews(result)
    if download:
        filename = "audit-%s%s.json" % (
            audit_id,
            ("-" + package.strip().lower().replace("/", "_")) if package else "",
        )
        return JSONResponse(
            result,
            headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
        )
    return result


@app.delete("/api/audits/{audit_id}", tags=["audit"], summary="删除某次审计记录")
def delete_audit(audit_id: int) -> Dict[str, Any]:
    if not database.delete_audit(audit_id):
        raise HTTPException(status_code=404, detail="审计记录不存在：%s" % audit_id)
    return {"deleted": audit_id}


# --- 跨审计复核标记 -----------------------------------------------------------


@app.post(
    "/api/reviews",
    tags=["review"],
    status_code=201,
    summary="创建复核标记（按冲突指纹跨审计生效）",
)
def create_review_mark(payload: ReviewMarkRequest) -> Dict[str, Any]:
    result = database.get_audit(payload.audit_id)
    if result is None:
        raise HTTPException(status_code=404, detail="审计记录不存在：%s" % payload.audit_id)
    conflict = next(
        (c for c in result.get("conflicts", []) if c.get("id") == payload.conflict_id),
        None,
    )
    if conflict is None:
        raise HTTPException(
            status_code=404,
            detail="审计 #%s 中不存在冲突/告警条目：%s" % (payload.audit_id, payload.conflict_id),
        )
    try:
        expires_at = review_mod.parse_expires_at(payload.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    fingerprint = review_mod.fingerprint_of(conflict)
    mark = database.save_review_mark(
        fingerprint=fingerprint,
        ancestor=conflict["ancestor"],
        node=conflict["node"],
        ancestor_license=licenses.normalize_license(conflict["ancestor_license"])[1],
        node_license=licenses.normalize_license(conflict["node_license"])[1],
        note=(payload.note or "").strip(),
        expires_at=expires_at,
    )
    return {"fingerprint": fingerprint, "mark": review_mod.public_mark(mark)}


@app.get("/api/reviews", tags=["review"], summary="复核标记记录列表（含已过期/已撤销）")
def list_review_marks() -> Dict[str, Any]:
    return {"items": [review_mod.public_mark(m) for m in database.list_review_marks()]}


@app.post("/api/reviews/{mark_id}/revoke", tags=["review"], summary="撤销复核标记（记录保留）")
def revoke_review_mark(mark_id: int) -> Dict[str, Any]:
    if not database.revoke_review_mark(mark_id):
        raise HTTPException(status_code=404, detail="复核标记不存在或已撤销：%s" % mark_id)
    mark = database.get_review_mark(mark_id)
    return {"revoked": mark_id, "mark": review_mod.public_mark(mark)}


@app.get("/api/matrix", tags=["meta"], summary="许可证兼容矩阵")
def license_matrix() -> Dict[str, Any]:
    return {
        "direction": "parent (分发作品许可证) -> dependency (被依赖代码许可证)",
        "categories": licenses.CATEGORY_LABELS,
        "category_notes": licenses.CATEGORY_NOTES,
        "matrix": licenses.build_matrix(),
    }


@app.get("/api/sample", tags=["meta"], summary="获取内置示例 manifest")
def sample_manifest() -> Dict[str, Any]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@app.get("/healthz", tags=["meta"], summary="健康检查")
def health() -> Dict[str, str]:
    return {"status": "ok"}


# --- 网页 -------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
