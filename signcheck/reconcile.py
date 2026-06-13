import json
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from .models import (
    EnrollmentRecord,
    SigninRecord,
    MatchRule,
    ReconcileResult,
)
from .storage import Storage
from .matcher import build_enrollment_lookup, find_match


def reconcile(storage: Storage) -> List[ReconcileResult]:
    enrollments = storage.get_all_enrollments()
    signins = storage.get_all_signins()
    rules = storage.get_all_rules()

    if not rules:
        rules = _default_rules()

    prev_results = storage.get_all_reconcile_results()
    prev_serialized = _serialize_results(prev_results)

    storage.clear_reconcile_results()

    enroll_by_session: Dict[str, List[EnrollmentRecord]] = defaultdict(list)
    for e in enrollments:
        enroll_by_session[e.session].append(e)

    signin_by_session: Dict[str, List[SigninRecord]] = defaultdict(list)
    for s in signins:
        signin_by_session[s.session].append(s)

    all_sessions = set(enroll_by_session.keys()) | set(signin_by_session.keys())
    results: List[ReconcileResult] = []

    for session in sorted(all_sessions):
        session_enrolls = enroll_by_session.get(session, [])
        session_signins = signin_by_session.get(session, [])

        lookup = build_enrollment_lookup(session_enrolls, rules)

        enroll_to_signins: Dict[int, List[SigninRecord]] = defaultdict(list)
        matched_signin_ids: set = set()

        for signin in session_signins:
            enroll = find_match(signin, lookup, rules, set(enroll_to_signins.keys()))
            if enroll is not None:
                enroll_to_signins[enroll.id].append(signin)
                matched_signin_ids.add(signin.id)
            else:
                results.append(ReconcileResult(
                    signin_id=signin.id,
                    name=signin.name,
                    phone=signin.phone,
                    session=session,
                    status="non_enrolled",
                ))

        for enroll in session_enrolls:
            if enroll.id in enroll_to_signins:
                signin_list = enroll_to_signins[enroll.id]
                results.append(ReconcileResult(
                    enroll_id=enroll.id,
                    signin_id=signin_list[0].id,
                    name=enroll.name,
                    phone=enroll.phone,
                    session=session,
                    status="normal",
                ))
                for signin in signin_list[1:]:
                    results.append(ReconcileResult(
                        enroll_id=enroll.id,
                        signin_id=signin.id,
                        name=signin.name,
                        phone=signin.phone,
                        session=session,
                        status="duplicate",
                    ))
            else:
                results.append(ReconcileResult(
                    enroll_id=enroll.id,
                    name=enroll.name,
                    phone=enroll.phone,
                    session=session,
                    status="absent",
                ))

    storage.add_reconcile_results(results)

    undo_data = json.dumps({
        "action": "reconcile",
        "prev_results": prev_serialized,
    }, ensure_ascii=False)
    storage.add_undo_action("reconcile", undo_data)

    return results


def _default_rules() -> List[MatchRule]:
    return [
        MatchRule(field_name="phone", match_type="exact", priority=1),
        MatchRule(field_name="name", match_type="exact", priority=2),
    ]


def _serialize_results(results: List[ReconcileResult]) -> List[dict]:
    return [
        {
            "id": r.id,
            "enroll_id": r.enroll_id,
            "signin_id": r.signin_id,
            "name": r.name,
            "phone": r.phone,
            "session": r.session,
            "status": r.status,
            "manual_mark": r.manual_mark,
            "notes": r.notes,
        }
        for r in results
    ]


def undo(storage: Storage) -> Optional[str]:
    action = storage.pop_last_undo_action()
    if action is None:
        return None

    data = json.loads(action.action_data)
    action_type = action.action_type

    if action_type == "import_enroll":
        ids = data.get("ids", [])
        storage.delete_enrollments_by_ids(ids)
        return f"import_enroll: 已删除 {len(ids)} 条报名记录"

    elif action_type == "import_signin":
        ids = data.get("ids", [])
        storage.delete_signins_by_ids(ids)
        return f"import_signin: 已删除 {len(ids)} 条签到记录"

    elif action_type == "import_rules":
        storage.clear_rules()
        prev_rules = data.get("prev_rules", [])
        for r in prev_rules:
            storage.add_rules([MatchRule(
                field_name=r["field_name"],
                match_type=r["match_type"],
                threshold=r.get("threshold"),
                priority=r["priority"],
            )])
        prev_mapping = data.get("prev_mapping", None)
        from .models import FieldMapping
        if prev_mapping:
            fm = FieldMapping(enroll=prev_mapping.get("enroll", {}), signin=prev_mapping.get("signin", {}))
            storage.save_field_mapping(fm)
        return f"import_rules: 已恢复 {len(prev_rules)} 条匹配规则"

    elif action_type == "reconcile":
        storage.clear_reconcile_results()
        prev_results = data.get("prev_results", [])
        for r in prev_results:
            storage.add_reconcile_results([ReconcileResult(
                enroll_id=r.get("enroll_id"),
                signin_id=r.get("signin_id"),
                name=r["name"],
                phone=r.get("phone"),
                session=r["session"],
                status=r["status"],
                manual_mark=r.get("manual_mark"),
                notes=r.get("notes"),
            )])
        return f"reconcile: 已恢复 {len(prev_results)} 条对账结果"

    elif action_type == "mark":
        result_id = data.get("result_id")
        prev_mark = data.get("prev_manual_mark")
        prev_notes = data.get("prev_notes")
        storage.update_reconcile_result_mark(result_id, prev_mark, prev_notes)
        return f"mark: 已撤销结果 #{result_id} 的标记"

    return None


def mark_result(storage: Storage, result_id: int, manual_mark: Optional[str], notes: Optional[str]) -> Optional[str]:
    result = storage.get_reconcile_result_by_id(result_id)
    if result is None:
        return None

    prev_mark = result.manual_mark
    prev_notes = result.notes

    undo_data = json.dumps({
        "result_id": result_id,
        "prev_manual_mark": prev_mark,
        "prev_notes": prev_notes,
    }, ensure_ascii=False)
    storage.add_undo_action("mark", undo_data)

    storage.update_reconcile_result_mark(result_id, manual_mark, notes)

    return f"已标记结果 #{result_id} 为「{manual_mark}」"
