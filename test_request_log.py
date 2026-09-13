import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from request_log import LoggedSession


class RetentionTests(unittest.TestCase):
    def test_cutoff_timezone_and_embedded_heading(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        cutoff = now - timedelta(weeks=4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.md"
            with LoggedSession(path) as session:
                old = (cutoff - timedelta(seconds=1)).isoformat()
                boundary = cutoff.astimezone(timezone(timedelta(hours=8))).isoformat()
                path.write_text(
                    f"# intro\n\n### {old} · expired\n\n```\nold payload\n```\n"
                    f"### {boundary} · boundary\n\n````\n"
                    f"### {old} · embedded heading\n```\n````\n"
                    f"### {now.isoformat()} · recent\n\n```\nnew payload\n```\n",
                    encoding="utf-8",
                )
                session.prune(now)
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# intro"))
                self.assertNotIn("· expired", text)
                self.assertNotIn("old payload", text)
                self.assertIn("· boundary", text)
                self.assertIn("· embedded heading", text)
                self.assertIn("new payload", text)

    def test_startup_and_periodic_cleanup(self):
        old = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.md"
            expired = f"### {old} · expired\n\n```\nold\n```\n"
            path.write_text(expired, encoding="utf-8")
            with LoggedSession(path) as session:
                self.assertNotIn("expired", path.read_text(encoding="utf-8"))
                path.write_text(expired, encoding="utf-8")
                session._next_cleanup = 0
                session.note("current", "new")
                self.assertNotIn("expired", path.read_text(encoding="utf-8"))
                with patch.object(session, "prune") as prune:
                    session.note("current", "new")
                    prune.assert_not_called()
