"""대화 로그 저장: logs/<대화ID>.jsonl(원본), .md(읽기용), .state.json(세션 ID 등 재시작용 상태)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class LogStore:
    def __init__(self, log_dir: Path, conv_id: str):
        self.dir = log_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.conv_id = conv_id
        self.jsonl = self.dir / f"{conv_id}.jsonl"
        self.md = self.dir / f"{conv_id}.md"
        self.state_file = self.dir / f"{conv_id}.state.json"

    def append(self, msg: dict, display_name: str) -> None:
        with open(self.jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        ts = datetime.fromtimestamp(msg["ts"]).strftime("%H:%M:%S")
        head = f"### {display_name}  `{ts}`"
        if msg.get("verdict"):
            head += f"  **[{msg['verdict']}]**"
        if msg.get("round"):
            head += f"  (라운드 {msg['round']})"
        body = msg["text"]
        if msg.get("kind") == "approval":
            body += f"\n\n```\n{msg['approval'].get('detail', '')}\n```"
        if msg.get("attachments"):
            body += "\n\n첨부: " + ", ".join(msg["attachments"])
        with open(self.md, "a", encoding="utf-8") as f:
            f.write(f"{head}\n\n{body}\n\n")

    def append_update(self, msg_id: str, fields: dict) -> None:
        """이미 기록한 메시지의 변경(예: 승인 카드 상태). 원본 줄은 그대로 두고 변경 줄을 덧붙인다."""
        with open(self.jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps({"_update": msg_id, "fields": fields}, ensure_ascii=False) + "\n")
        status = (fields.get("approval") or {}).get("status")
        if status:
            label = {"allowed": "허용함", "denied": "거부함"}.get(status, status)
            with open(self.md, "a", encoding="utf-8") as f:
                f.write(f"> 승인 요청 {msg_id}: **{label}**\n\n")

    def load_messages(self) -> list[dict]:
        if not self.jsonl.exists():
            return []
        out, by_id = [], {}
        with open(self.jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if "_update" in rec:
                    if rec["_update"] in by_id:
                        by_id[rec["_update"]].update(rec["fields"])
                    continue
                out.append(rec)
                by_id[rec.get("id")] = rec
        return out

    def save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def load_state(self) -> dict | None:
        if not self.state_file.exists():
            return None
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    @staticmethod
    def latest_conv_id(log_dir: Path) -> str | None:
        states = sorted(log_dir.glob("*.state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return states[0].name[: -len(".state.json")] if states else None
