from typing import List, Optional, Dict, Tuple
from .models import EnrollmentRecord, SigninRecord, MatchRule


def match_record(
    signin: SigninRecord,
    enrollment: EnrollmentRecord,
    rules: List[MatchRule],
) -> bool:
    for rule in sorted(rules, key=lambda r: r.priority):
        signin_val = getattr(signin, rule.field_name, None)
        enroll_val = getattr(enrollment, rule.field_name, None)
        if signin_val is None or enroll_val is None:
            continue
        if rule.match_type == "exact":
            if str(signin_val).strip() == str(enroll_val).strip():
                return True
        elif rule.match_type == "contains":
            if str(enroll_val).strip() in str(signin_val).strip():
                return True
        elif rule.match_type == "fuzzy":
            threshold = rule.threshold or 0.8
            if _similarity(str(signin_val).strip(), str(enroll_val).strip()) >= threshold:
                return True
    return False


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    len_a, len_b = len(a), len(b)
    if len_a == 0 or len_b == 0:
        return 0.0
    matrix = [[0] * (len_b + 1) for _ in range(len_a + 1)]
    for i in range(len_a + 1):
        matrix[i][0] = i
    for j in range(len_b + 1):
        matrix[0][j] = j
    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost,
            )
    distance = matrix[len_a][len_b]
    max_len = max(len_a, len_b)
    return 1.0 - distance / max_len


def build_enrollment_lookup(
    enrollments: List[EnrollmentRecord],
    rules: List[MatchRule],
) -> Dict[str, List[EnrollmentRecord]]:
    lookup: Dict[str, List[EnrollmentRecord]] = {}
    sorted_rules = sorted(rules, key=lambda r: r.priority)
    for enroll in enrollments:
        for rule in sorted_rules:
            val = getattr(enroll, rule.field_name, None)
            if val is not None and str(val).strip():
                key = f"{rule.field_name}:{str(val).strip()}"
                lookup.setdefault(key, []).append(enroll)
    return lookup


def find_match(
    signin: SigninRecord,
    lookup: Dict[str, List[EnrollmentRecord]],
    rules: List[MatchRule],
    already_matched_enroll_ids: set,
) -> Optional[EnrollmentRecord]:
    sorted_rules = sorted(rules, key=lambda r: r.priority)
    for rule in sorted_rules:
        val = getattr(signin, rule.field_name, None)
        if val is None or not str(val).strip():
            continue
        key = f"{rule.field_name}:{str(val).strip()}"
        candidates = lookup.get(key, [])
        for enroll in candidates:
            if match_record(signin, enroll, rules):
                return enroll
    return None
