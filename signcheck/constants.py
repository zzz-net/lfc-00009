STATUS_LABELS = {
    "normal": "正常签到",
    "absent": "缺席",
    "non_enrolled": "非报名人员",
    "duplicate": "重复扫码",
}

VALID_STATUSES = {"normal", "absent", "non_enrolled", "duplicate"}

DEFAULT_ENROLL_MAPPING = {
    "name": "姓名",
    "phone": "手机号",
    "session": "场次",
}

DEFAULT_SIGNIN_MAPPING = {
    "name": "姓名",
    "phone": "手机号",
    "session": "场次",
    "scan_time": "扫码时间",
}

STATUS_ORDER = ["normal", "absent", "non_enrolled", "duplicate"]

STATUS_COLORS = {
    "normal": "#28a745",
    "absent": "#dc3545",
    "non_enrolled": "#ffc107",
    "duplicate": "#6c757d",
}

CSV_EXPORT_HEADERS = ["ID", "姓名", "手机号", "场次", "状态", "标记", "备注"]

HANDOFF_CSV_ENROLL_HEADERS = ["ID", "姓名", "手机号", "场次", "来源文件", "来源行号"]
HANDOFF_CSV_SIGNIN_HEADERS = ["ID", "姓名", "手机号", "场次", "扫码时间", "来源文件", "来源行号"]
HANDOFF_CSV_RESULT_HEADERS = ["ID", "报名ID", "签到ID", "姓名", "手机号", "场次", "状态", "状态标签", "人工标记", "备注"]

STATS_EXPORT_HEADERS = [
    "场次", "总计",
    "正常签到", "正常签到占比",
    "缺席", "缺席占比",
    "非报名人员", "非报名人员占比",
    "重复扫码", "重复扫码占比",
]
