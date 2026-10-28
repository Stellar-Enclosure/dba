import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ha_probe.py"


class HaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.evidence = root / "evidence.tsv"
        passfile = root / "pgpass"
        passfile.write_text("test-only\n")
        passfile.chmod(0o600)
        ca = root / "ca.pem"
        ca.write_text("test-only\n")
        fake = root / "psql"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import os, re, sys\n"
            "query = sys.argv[sys.argv.index('-c') + 1]\n"
            "if 'to_regclass' in query:\n"
            "    print(os.environ.get('TEST_PREFLIGHT', 'app|pg-test|f|t|on|on|t'))\n"
            "elif query.startswith('SELECT seq'):\n"
            "    print(os.environ.get('TEST_ROWS', '').replace(',', '\\n'))\n"
            "else:\n"
            "    m = re.search(r\"VALUES \\('[^']+', (\\d+)\\)\", query)\n"
            "    sys.exit(1 if os.environ.get('TEST_ALL_UNKNOWN') == '1' or (m and m.group(1) == '2') else 0)\n"
        )
        fake.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(root) + os.pathsep + self.env.get("PATH", ""),
            "PGHOST": "test.invalid",
            "PGDATABASE": "app",
            "PGUSER": "tester",
            "PGPASSFILE": str(passfile),
            "PGSSLROOTCERT": str(ca),
            "PGSSLMODE": "verify-full",
        })
        self.env.pop("PGPASSWORD", None)
        self.env.pop("PGSERVICE", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_probe(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--expected-cluster", "pg-test"], env=self.env,
            text=True, capture_output=True, check=False,
        )

    def write_probe(self):
        return self.run_probe("write", "--expected-db", "app", "--evidence", str(self.evidence),
                              "--test-only", "--count", "3", "--interval", "0")

    def test_acknowledged_and_unknown_transactions_are_distinguished(self):
        result = self.write_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.evidence.open() as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        self.assertEqual([row["outcome"] for row in rows], ["ack", "unknown", "ack"])
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o600)
        self.env["TEST_ROWS"] = "1,2,3"
        result = self.run_probe("verify", "--expected-db", "app", "--evidence", str(self.evidence))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("missing_ack=0", result.stdout)
        self.assertIn("unknown_present=1", result.stdout)

    def test_missing_acknowledged_transaction_fails_verification(self):
        self.assertEqual(self.write_probe().returncode, 0)
        self.env["TEST_ROWS"] = "1,2"
        result = self.run_probe("verify", "--expected-db", "app", "--evidence", str(self.evidence))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_ack=1", result.stdout)

    def test_incomplete_evidence_is_not_accepted(self):
        self.assertEqual(self.write_probe().returncode, 0)
        lines = self.evidence.read_text().splitlines()
        self.evidence.write_text("\n".join(lines[:-1]) + "\n")
        result = self.run_probe("verify", "--expected-db", "app", "--evidence", str(self.evidence))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("证据不完整", result.stderr)

    def test_requires_test_flag_and_does_not_overwrite_evidence(self):
        result = self.run_probe("write", "--expected-db", "app", "--evidence", str(self.evidence))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.evidence.exists())
        self.assertEqual(self.write_probe().returncode, 0)
        before = self.evidence.read_bytes()
        self.assertNotEqual(self.write_probe().returncode, 0)
        self.assertEqual(self.evidence.read_bytes(), before)

    def test_rejects_bad_connection_policy_before_creating_evidence(self):
        self.env["PGSSLMODE"] = "require"
        result = self.write_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.evidence.exists())

    def test_rejects_service_and_unix_socket_connections(self):
        self.env["PGSERVICE"] = "test"
        self.assertNotEqual(self.write_probe().returncode, 0)
        self.env.pop("PGSERVICE")
        self.env["PGHOST"] = "/tmp"
        self.assertNotEqual(self.write_probe().returncode, 0)
        self.assertFalse(self.evidence.exists())

    def test_rejects_missing_synchronous_configuration(self):
        self.env["TEST_PREFLIGHT"] = "app|pg-test|f|t|on|on|f"
        result = self.write_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.evidence.exists())

    def test_unexpected_database_rows_fail_verification(self):
        self.assertEqual(self.write_probe().returncode, 0)
        self.env["TEST_ROWS"] = "1,2,3,4"
        result = self.run_probe("verify", "--expected-db", "app", "--evidence", str(self.evidence))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected=1", result.stdout)

    def test_all_unknown_writes_are_not_reported_as_success(self):
        self.env["TEST_ALL_UNKNOWN"] = "1"
        result = self.write_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ack=0", result.stdout)
        self.assertTrue(self.evidence.exists())

    def test_rejects_unexpected_cluster_before_writes(self):
        self.env["TEST_PREFLIGHT"] = "app|other-cluster|f|t|on|on|t"
        result = self.write_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.evidence.exists())


if __name__ == "__main__":
    unittest.main()
