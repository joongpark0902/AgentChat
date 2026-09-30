"""PC 안에서 하는 음성 인식(faster-whisper). 음성은 이 PC 밖으로 나가지 않는다.

2026-09-28 실측(Windows 11, CPU 12스레드, int8, 6.5초 한국어 음성):
- small: 9.8초, 정확 / base: 3.0초, 1곳 오인식("첨부한"→"천부한")
- 용어 힌트(initial_prompt)가 없으면 "손익"→"손이", "영업이익"→"영업이 입" → 힌트 필수
- 모델 첫 다운로드를 HF 캐시(심볼릭 링크)로 하면 WinError 1314 가 났음 → models/ 폴더에 직접 받는다(download_model output_dir)
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from .config import ROOT

MODEL_DIR = ROOT / "models"
MIME_EXT = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
            "audio/mp4": ".m4a", "audio/mpeg": ".mp3"}


def build_prompt(vocab: list[str], names: list[str]) -> str:
    words = [w for w in [*vocab, *names] if w]
    return "회계·감사 업무 대화입니다. " + ", ".join(words) + "." if words else ""


class Transcriber:
    def __init__(self):
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()
        self.status = "idle"  # idle | downloading | loading | ready | error

    def model_path(self, size: str) -> Path:
        return MODEL_DIR / f"whisper-{size}"

    def is_downloaded(self, size: str) -> bool:
        return (self.model_path(size) / "model.bin").exists()

    def load(self, size: str):
        with self._lock:
            if size in self._models:
                return self._models[size]
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model

            if not self.is_downloaded(size):
                self.status = "downloading"
                download_model(size, output_dir=str(self.model_path(size)))
            self.status = "loading"
            model = WhisperModel(str(self.model_path(size)), device="cpu", compute_type="int8",
                                 cpu_threads=os.cpu_count() or 0)
            self._models[size] = model
            self.status = "ready"
            return model

    def transcribe(self, audio: bytes, mime: str, size: str, prompt: str) -> dict:
        model = self.load(size)
        ext = MIME_EXT.get((mime or "").split(";")[0].strip(), ".webm")
        fd, path = tempfile.mkstemp(suffix=ext, prefix="agentchat_stt_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
            t0 = time.time()
            segs, info = model.transcribe(path, language="ko", beam_size=1, vad_filter=True,
                                          initial_prompt=prompt or None)
            text = "".join(s.text for s in segs).strip()
            return {"text": text, "seconds": round(time.time() - t0, 1), "audio_seconds": round(info.duration, 1),
                    "model": size}
        finally:
            try:
                os.remove(path)  # 임시 녹음 파일은 인식 후 바로 지움(원본 녹음은 남기지 않음)
            except OSError:
                pass
