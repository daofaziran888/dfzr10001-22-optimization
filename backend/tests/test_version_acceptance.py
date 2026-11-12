"""模板版本化验收测试（验收序列 V 及其余规则）。

不依赖 pytest/httpx，只用标准库 unittest 直连服务层；每个用例前用种子数据
重置内存仓库，保证互不干扰。
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.store import store
from app.versions import list_diagnostics, parse_snapshot, registry


def reset_store() -> None:
    store._tables = {
        name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()
    }


class VersionAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    # 验收序列 V -------------------------------------------------------
    def test_sequence_v_default_value_pin_and_detail_versions(self) -> None:
        # PARS-0002：版本1默认值0，版本2改成100
        self.assertEqual(registry.get_snapshot("PARS-0002", 1)["默认值"]["金额"], 0)
        self.assertEqual(registry.get_snapshot("PARS-0002", 2)["默认值"]["金额"], 100)
        self.assertEqual(registry.effective_version("PARS-0002"), 2)

        # 旧任务 TASK-0002 启动时钉住版本1，空输入仍套用默认值 0
        old_task = store.find("task", 2)
        self.assertEqual(old_task["模板版本"], 1)
        snapshot, error = registry.task_snapshot(old_task)
        self.assertIsNone(error)
        old_result = parse_snapshot(snapshot, {}, task_code=old_task["任务编号"])
        self.assertEqual(old_result["模板版本"], 1)
        self.assertEqual(old_result["数据"]["金额"], 0)

        # 新任务创建即钉住当前生效版本2，空输入套用默认值 100
        from app.services.task import TaskService
        entry, missing = TaskService().create_entry({
            "任务编号": "TASK-0010", "任务名称": "新任务", "关联源端": "S1",
            "解析模板编号": "PARS-0002",
        })
        self.assertEqual(missing, [])
        self.assertEqual(entry["模板版本"], 2)
        new_task = store.find("task", entry["id"])
        new_snapshot, _ = registry.task_snapshot(new_task)
        new_result = parse_snapshot(new_snapshot, {}, task_code="TASK-0010")
        self.assertEqual(new_result["模板版本"], 2)
        self.assertEqual(new_result["数据"]["金额"], 100)

        # PARS-0003 详情显示各自实际版本：生效v1、草稿v2、切换失败v3
        from app.services.parser import ParserService
        detail = ParserService().get_entry(3)
        self.assertEqual(detail["模板编号"], "PARS-0003")
        self.assertEqual(detail["生效版本号"], 1)
        statuses = {row["版本号"]: row["状态"] for row in detail["版本档案"]}
        self.assertEqual(statuses, {1: "已启用", 2: "草稿", 3: "切换失败"})
        effective_flags = [row["是否生效"] for row in detail["版本档案"]]
        self.assertEqual(sum(1 for flag in effective_flags if flag), 1)

        # TASK-0003 实际读取版本仍是启动时的 v1
        task3 = store.find("task", 3)
        self.assertEqual(task3["模板版本"], 1)
        snap3, _ = registry.task_snapshot(task3)
        self.assertEqual(snap3["模板版本"], 1)

    def test_old_task_must_not_use_new_regex(self) -> None:
        # 版本2新增 amount 校验正则 ^\d+$；旧任务版本1没有该正则
        old_task = store.find("task", 2)
        snapshot, _ = registry.task_snapshot(old_task)
        self.assertEqual(snapshot["校验正则"], {})
        old_result = parse_snapshot(snapshot, {"amount": "-5"}, task_code="TASK-0002")
        self.assertTrue(old_result["通过"], "旧任务不能被新增正则拦截")
        self.assertEqual(old_result["数据"]["金额"], -5)

        v2 = registry.get_snapshot("PARS-0002", 2)
        new_result = parse_snapshot(v2, {"amount": "-5"}, task_code="TASK-0010")
        self.assertFalse(new_result["通过"])
        self.assertTrue(any("校验正则" in msg for msg in new_result["错误"]))

    # 切换失败回滚 ------------------------------------------------------
    def test_switch_failure_rolls_back_to_last_available(self) -> None:
        self.assertEqual(registry.effective_version("PARS-0003"), 1)
        target, message, locked = registry.switch_version("PARS-0003", 3)
        self.assertTrue(locked)
        self.assertEqual(target["状态"], "切换失败")
        self.assertIn("回滚到版本 1", message)
        # 上一可用版本仍然生效，parser 表镜像没有被污染
        self.assertEqual(registry.effective_version("PARS-0003"), 1)
        parser_row = next(row for row in store.rows("parser") if row["模板编号"] == "PARS-0003")
        self.assertEqual(parser_row["模板版本"], 1)
        self.assertNotIn("等级", parser_row["默认值"])
        # 任务工作台出现版本失败待办
        todos = registry.list_todos()
        self.assertTrue(any(
            t["模板编号"] == "PARS-0003" and t["版本号"] == 3 and "回滚" in t["说明"]
            for t in todos
        ))
        # 审计记录留痕且标记异常
        audits = [row for row in store.rows("audit") if row.get("操作对象") == "PARS-0003#v3"]
        self.assertTrue(audits)
        self.assertTrue(audits[-1]["abnormal"])

    # 并发切换只允许一个版本生效 -----------------------------------------
    def test_concurrent_switch_only_one_takes_effect(self) -> None:
        import app.versions as versions_mod

        good_values = {
            "输入格式": "json", "输出格式": "json",
            "字段映射": {"amount": "金额"}, "类型转换": {"金额": "int"},
            "默认值": {"金额": 0}, "校验正则": {},
        }
        draft3, _ = registry.create_draft("PARS-0001", good_values)
        draft4, _ = registry.create_draft("PARS-0001", good_values)
        self.assertEqual((draft3["版本号"], draft4["版本号"]), (3, 4))
        self.assertEqual(registry.effective_version("PARS-0001"), 2)

        # 模拟一个切换在途：其它线程已经持锁且尚未释放。
        versions_mod._switch_lock.acquire()
        try:
            target, message, locked = registry.switch_version("PARS-0001", 4)
            self.assertIsNone(target)
            self.assertFalse(locked, "锁被占时并发切换必须被拒绝")
            self.assertIn("已有模板版本切换在途", message)
            # 被拒绝期间生效版本不变，且只有一个生效版本
            self.assertEqual(registry.effective_version("PARS-0001"), 2)
            self.assertTrue(any(t["待办类型"] == "并发冲突" for t in registry.list_todos()))
        finally:
            versions_mod._switch_lock.release()

        # 锁释放后切换才能成功，最终全模板仍只有一个生效版本
        target, message, locked = registry.switch_version("PARS-0001", 3)
        self.assertTrue(locked)
        self.assertEqual(target["状态"], "已启用")
        effective = [
            row["版本号"] for row in registry.list_versions("PARS-0001") if row["是否生效"]
        ]
        self.assertEqual(effective, [3])

    # 字段重命名兼容既有映射 ---------------------------------------------
    def test_field_rename_alias_keeps_old_mapping(self) -> None:
        # PARS-0001 v2：源字段 user_name 改名为 user，改名记录保存在版本档案
        v2 = registry.get_snapshot("PARS-0001", 2)
        self.assertEqual(v2["字段改名记录"], [
            {"源字段": "user_name", "新源字段": "user", "目标字段": "用户名"}
        ])
        # 旧输入仍使用旧源字段名，别名链自动兼容，目标字段照常产出
        result = parse_snapshot(v2, {"amount": "12", "user_name": "alice"}, task_code="预览")
        self.assertEqual(result["数据"]["用户名"], "alice")
        latest_diag = store.rows("parse_diagnostic")[-1]
        note = next(n for n in latest_diag["字段解释"] if n["目标字段"] == "用户名")
        self.assertEqual(note["实际命中源字段"], "user_name")
        self.assertTrue(note["使用历史别名"])
        # 新源字段名同样可用
        result_new = parse_snapshot(v2, {"amount": "12", "user": "bob"}, task_code="预览")
        self.assertEqual(result_new["数据"]["用户名"], "bob")
        # 旧版本 v1 直接按旧名读取，不依赖别名
        v1 = registry.get_snapshot("PARS-0001", 1)
        result_old = parse_snapshot(v1, {"amount": "12", "user_name": "carol"}, task_code="TASK-0001")
        self.assertEqual(result_old["数据"]["用户名"], "carol")

    # 历史诊断与旧字段解释保持原样 ---------------------------------------
    def test_historical_diagnosis_is_immutable_after_switch(self) -> None:
        old_task = store.find("task", 2)
        snapshot, _ = registry.task_snapshot(old_task)
        first = parse_snapshot(snapshot, {"amount": "7"}, task_code="TASK-0002")
        diag_id = first["诊断编号"]
        before = copy.deepcopy(
            next(d for d in list_diagnostics(task_code="TASK-0002") if d["id"] == diag_id)
        )

        # 随后模板切换（PARS-0002 无新版本，造一个 v3 再切）
        draft, _ = registry.create_draft("PARS-0002", {
            "输入格式": "json", "输出格式": "json",
            "字段映射": {"amount": "金额", "remark": "备注", "level": "等级"},
            "类型转换": {"金额": "int"}, "默认值": {"金额": 999},
            "校验正则": {"amount": r"^\d{1,2}$"},
        })
        target, _, locked = registry.switch_version("PARS-0002", draft["版本号"])
        self.assertTrue(locked)
        self.assertEqual(target["状态"], "已启用")

        after = next(d for d in list_diagnostics(task_code="TASK-0002") if d["id"] == diag_id)
        self.assertEqual(before, after, "历史诊断不允许被后续切换改写")
        self.assertEqual(after["模板版本"], 1)
        self.assertEqual(after["字段解释"][0]["版本内源字段"], "amount")

    # 任务详情必须指明实际生效版本 ---------------------------------------
    def test_task_detail_shows_pinned_and_effective_version(self) -> None:
        from app.services.task import TaskService
        detail = TaskService().get_entry(2)
        self.assertEqual(detail["模板版本"], 1)
        self.assertEqual(detail["当前模板生效版本"], 2)
        self.assertTrue(detail["版本落后"])

    def test_parser_row_mirrors_effective_version(self) -> None:
        row = next(r for r in store.rows("parser") if r["模板编号"] == "PARS-0002")
        self.assertEqual(row["模板版本"], 2)
        self.assertEqual(row["默认值"]["金额"], 100)
        self.assertEqual(row["校验正则"], {"amount": r"^\d+$"})

    def test_successful_switch_syncs_parser_row_and_audit(self) -> None:
        target, message, locked = registry.switch_version("PARS-0003", 2)
        self.assertTrue(locked)
        self.assertEqual(target["状态"], "已启用")
        self.assertEqual(registry.effective_version("PARS-0003"), 2)
        row = next(r for r in store.rows("parser") if r["模板编号"] == "PARS-0003")
        self.assertEqual(row["模板版本"], 2)
        self.assertEqual(row["默认值"].get("等级"), "normal")
        audits = [r for r in store.rows("audit") if r.get("操作对象") == "PARS-0003#v2"]
        self.assertTrue(audits)
        self.assertFalse(audits[-1]["abnormal"])

    def test_missing_snapshot_returns_readable_error(self) -> None:
        broken_task = {"任务编号": "TASK-X", "解析模板编号": "PARS-0002", "模板版本": 99}
        snapshot, error = registry.task_snapshot(broken_task)
        self.assertIsNone(snapshot)
        self.assertIn("版本 99 已不存在", error)

    def test_route_order_prefers_static_version_paths(self) -> None:
        from app.main import app

        # FastAPI 新版把 include_router 包成 _IncludedRouter，需要展开原始路由。
        def flatten(route: object) -> list[str]:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                paths: list[str] = []
                for sub in inner.routes:
                    paths.extend(flatten(sub))
                return paths
            path = getattr(route, "path", None)
            return [path] if path else []

        paths: list[str] = []
        for route in app.routes:
            paths.extend(flatten(route))
        self.assertIn("/api/parser/todos", paths)
        self.assertLess(paths.index("/api/parser/todos"), paths.index("/api/parser/{entry_id}"))
        self.assertLess(
            paths.index("/api/task/version-todos"), paths.index("/api/task/{entry_id}")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
