"""启动时缺少可选音频分析依赖，应能提供 API 并安全跳过韵律评分。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BACKEND = Path(__file__).resolve().parents[1]


class OptionalAudioStartupTest(unittest.TestCase):
    def test_startup_without_optional_audio_dependencies(self):
        script = """
import sys
import types

if sys.argv[1] == 'numpy':
    sys.modules['numpy'] = None
else:
    # Make numpy importable so this case specifically exercises missing librosa.
    sys.modules['numpy'] = types.ModuleType('numpy')
    sys.modules['librosa'] = None

from fastapi.testclient import TestClient
from app.main import app
from app.services import prosody

assert prosody.score(b'', True) is None
assert prosody.score(b'', False) is None
with TestClient(app) as client:
    assert client.get('/api/health').json()['status'] == 'ok'
"""
        for missing in ("numpy", "librosa"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as data_dir:
                env = os.environ.copy()
                env["DATA_DIR"] = data_dir
                result = subprocess.run(
                    [sys.executable, "-c", script, missing],
                    cwd=BACKEND,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
