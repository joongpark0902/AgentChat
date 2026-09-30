"""테스트는 개인 설정(config/agents.yaml)이 아니라 배포용 예시 설정으로 돈다(어느 PC 에서 돌려도 같은 결과).
예시는 모델·호칭을 비워 두므로, 기존 테스트가 기대하는 값(모델 고정, 호칭)만 여기서 채운다."""
import atexit
import shutil
import tempfile
from pathlib import Path

import yaml

import app.config as appconfig

_FIXTURE = Path(tempfile.mkdtemp(prefix="agentchat_testcfg_"))
atexit.register(shutil.rmtree, _FIXTURE, ignore_errors=True)

_src = appconfig.ROOT / "config"
_agents = yaml.safe_load((_src / "agents.example.yaml").read_text(encoding="utf-8"))
_agents["user"]["name"] = "사장님"
_agents["agents"]["jake"]["model"] = "claude-opus-5-5"
_agents["agents"]["clara"]["model"] = "claude-opus-5-5"
_agents["agents"]["quinn"]["model"] = "gpt-6-sol"
(_FIXTURE / "agents.yaml").write_text(yaml.safe_dump(_agents, allow_unicode=True, sort_keys=False), encoding="utf-8")
shutil.copy(_src / "settings.example.yaml", _FIXTURE / "settings.yaml")

appconfig.CONFIG_DIR = _FIXTURE
