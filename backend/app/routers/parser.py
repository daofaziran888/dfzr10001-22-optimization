"""数据解析模板接口：维护解析模板，覆盖提交校验、启用配置、停用配置等动作。

版本化相关接口（版本档案、切换、失败待办、按版本试跑）声明在 /{entry_id}
之前，避免被整数明细路由提前拦截。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.parser import ParserService
from app.versions import parse_snapshot

router = APIRouter(prefix="/api/parser", tags=["数据解析模板"])

service = ParserService()

LIST_FIELDS = ["模板编号", "输入格式", "字段映射", "类型转换", "默认值", "校验正则", "输出格式", "模板版本"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤数据解析模板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/todos")
def list_version_todos(
    include_closed: bool = Query(default=False, description="是否包含已处理待办"),
) -> dict[str, Any]:
    """版本失败待办：切换失败或并发冲突时生成，供任务工作台/模板页展示。"""
    items = service.list_todos(include_closed=include_closed)
    return {"total": len(items), "items": items}


@router.post("/todos/{todo_id}/dismiss", response_model=ActionResult)
def dismiss_version_todo(todo_id: int) -> ActionResult:
    """关闭一条版本失败待办；不影响版本档案本身。"""
    todo, message = service.dismiss_todo(todo_id)
    if todo is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=todo)


@router.get("/code/{template_code}/versions")
def list_template_versions(template_code: str) -> dict[str, Any]:
    """返回模板的全部不可变版本档案，状态覆盖草稿/待校验/已启用/已停用/切换失败。"""
    if service.get_template_by_code(template_code) is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {template_code} 不存在或已归档")
    items = service.list_versions(template_code)
    effective = next((row["版本号"] for row in items if row.get("是否生效")), None)
    return {"模板编号": template_code, "生效版本号": effective, "total": len(items), "items": items}


@router.get("/code/{template_code}/versions/{version}")
def get_template_version(template_code: str, version: int) -> dict[str, Any]:
    """读取指定版本快照；任务排查时用它核对“任务实际读取版本”。"""
    row = service.get_version(template_code, version)
    if row is None:
        raise HTTPException(status_code=404, detail=f"模板 {template_code} 不存在版本 {version}")
    return row


@router.post("/code/{template_code}/versions", response_model=ActionResult)
def create_template_version(template_code: str, payload: EntryPayload) -> ActionResult:
    """起草新版本（草稿，不直接生效）；可携带字段改名记录兼容既有映射。"""
    if service.get_template_by_code(template_code) is None:
        return ActionResult(ok=False, message=f"解析模板 {template_code} 不存在或已归档")
    values = payload.values
    rename_records = values.pop("字段改名记录", None)
    draft, message = service.create_draft(template_code, values, rename_records)
    if draft is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=f"模板 {template_code} 新版本草稿已登记，待切换校验", entry=draft)


@router.post("/code/{template_code}/switch", response_model=ActionResult)
def switch_template_version(template_code: str, payload: EntryPayload) -> ActionResult:
    """切换生效版本：并发只允许一个版本生效，失败自动回滚上一可用版本并生成待办。"""
    version = payload.values.get("版本号")
    if version is None:
        return ActionResult(ok=False, message="缺少参数：版本号")
    try:
        version = int(version)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message=f"版本号必须是整数，收到：{version!r}")
    entry, message, locked = service.switch_version(
        template_code, version, operator=str(payload.values.get("操作人") or "系统"),
    )
    if entry is None:
        return ActionResult(ok=locked, message=message)
    return ActionResult(ok=entry.get("状态") != "切换失败", message=message, entry=entry)


@router.post("/code/{template_code}/versions/{version}/parse", response_model=ActionResult)
def parse_with_version(template_code: str, version: int, payload: EntryPayload) -> ActionResult:
    """按指定版本试跑解析：所有规则均取自该版本快照，诊断按该版本号回写。"""
    from app.versions import registry

    snapshot = registry.get_snapshot(template_code, version)
    if snapshot is None:
        return ActionResult(ok=False, message=f"模板 {template_code} 不存在版本 {version}")
    data = payload.values.get("数据")
    if not isinstance(data, dict):
        return ActionResult(ok=False, message="缺少待解析数据：values.数据 必须是对象")
    result = parse_snapshot(snapshot, data, task_code=f"预览:{template_code}")
    return ActionResult(ok=bool(result["通过"]), message="版本试跑完成", entry=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析模板执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据解析模板清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "parser", "total": total, "items": items}
