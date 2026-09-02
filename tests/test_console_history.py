"""Console audit source tagging."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.history import begin_mesh_audit, finish_mesh_audit, insert_command, open_history


class ConsoleHistoryTests(unittest.TestCase):
    def test_command_and_mesh_audit_source_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            insert_command(
                conn,
                unit="me0032",
                argv="ota stats",
                reply="ok",
                ok=True,
                source="console",
            )
            audit_id = begin_mesh_audit(
                conn,
                unit="me0032",
                kind="cli",
                label="ota stats",
                attempt=1,
                path="flood",
                wait_s=9.0,
                source="console",
            )
            finish_mesh_audit(conn, audit_id, ok=True, outcome="ok", reply="ok")
            row = conn.execute(
                "SELECT source FROM commands WHERE unit = ?", ("me0032",)
            ).fetchone()
            audit = conn.execute(
                "SELECT source FROM mesh_audit WHERE id = ?", (audit_id,)
            ).fetchone()
            self.assertEqual(row["source"], "console")
            self.assertEqual(audit["source"], "console")


if __name__ == "__main__":
    unittest.main()
