from __future__ import annotations

import json
from pathlib import Path


Path("worker_output.txt").write_text("Stage A worker artifact\n", encoding="utf-8")
print(json.dumps({"type": "log", "message": "worker created worker_output.txt"}))
print(json.dumps({"type": "test_result", "name": "demo_contract", "passed": True}))
print(json.dumps({"type": "worker_submitted", "summary": "demo node ready for evidence review"}))
