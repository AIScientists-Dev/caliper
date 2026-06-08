"""Smoke test for the web app (offline, mock model). Skipped if fastapi absent."""
import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAVE_FASTAPI = False

HERE = os.path.dirname(__file__)
EXAMPLES = os.path.abspath(os.path.join(HERE, "..", "examples"))


@unittest.skipUnless(_HAVE_FASTAPI, "fastapi not installed")
class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CALIPER_PROVIDER"] = "mock"
        os.environ["CALIPER_DATA_ROOT"] = EXAMPLES
        os.environ["CALIPER_WORKSPACE"] = tempfile.mkdtemp()
        os.environ.pop("CALIPER_WEB_PASSWORD", None)  # dev-open
        from caliper.web.server import app
        cls.client = TestClient(app)

    def test_index_and_whoami(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("data_root", self.client.get("/api/whoami").json())

    def test_browse_confined(self):
        r = self.client.get("/api/browse", params={"path": "data"})
        self.assertEqual(r.status_code, 200)
        names = [e["name"] for e in r.json()["entries"]]
        self.assertIn("counts.csv", names)
        # cannot escape the data root
        self.assertEqual(self.client.get("/api/browse", params={"path": "../../.."}).status_code, 400)

    def test_chat_streams_to_decision(self):
        csv = os.path.join(EXAMPLES, "data", "counts.csv")
        with self.client.stream("POST", "/api/chat",
                                json={"message": "find DE genes", "data_paths": [csv]}) as r:
            body = "".join(chunk for chunk in r.iter_text())
        self.assertIn('"type": "plan"', body)
        self.assertIn('"type": "decision"', body)


if __name__ == "__main__":
    unittest.main()
