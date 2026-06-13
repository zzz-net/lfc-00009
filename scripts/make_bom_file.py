import os

json_content = """{
  "field_mapping": {
    "enroll": {
      "name": "姓名",
      "phone": "手机号",
      "session": "场次"
    },
    "signin": {
      "name": "姓名",
      "phone": "手机号",
      "session": "场次",
      "scan_time": "扫码时间"
    }
  },
  "match_rules": [
    {"field": "phone", "match_type": "exact", "priority": 1},
    {"field": "name", "match_type": "exact", "priority": 2}
  ]
}
"""

sample_dir = os.path.join(os.path.dirname(__file__), "..", "sample_data")
filepath = os.path.join(sample_dir, "rules_bom.json")

with open(filepath, "wb") as f:
    f.write(b"\xef\xbb\xbf")
    f.write(json_content.encode("utf-8"))

print(f"Created {filepath} with UTF-8 BOM")
