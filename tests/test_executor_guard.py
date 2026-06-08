"""Tests for the confined-workspace executor and its safety guard."""
import os
import tempfile
import unittest

from caliper.core.executor import Executor, check_code


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_runs_and_writes_inside_workspace(self):
        ex = Executor(workspace=self.ws, confine="guard")
        r = ex.run("open('out.txt','w').write('hi'); print('CALIPER_RESULT:{\"ok\":1}')")
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("CALIPER_RESULT", r.stdout)
        hits = [root for root, _, files in os.walk(self.ws) if "out.txt" in files]
        self.assertTrue(hits and hits[0].startswith(self.ws))  # output confined to workspace

    def test_blocks_rmtree(self):
        r = Executor(workspace=self.ws, confine="guard").run("import shutil; shutil.rmtree('/etc')")
        self.assertTrue(r.blocked)
        self.assertFalse(r.ok)

    def test_blocks_rm_rf(self):
        r = Executor(workspace=self.ws, confine="guard").run("import os; os.system('rm -rf /home/data')")
        self.assertTrue(r.blocked)

    def test_blocks_write_to_readonly_input(self):
        ex = Executor(workspace=self.ws, readonly_inputs=["/data/raw.fastq"], confine="guard")
        r = ex.run("import os; os.remove('/data/raw.fastq')")
        self.assertTrue(r.blocked)

    def test_allows_reading_an_input(self):
        inp = os.path.join(self.ws, "in.txt")
        open(inp, "w").write("payload")
        ex = Executor(workspace=self.ws, readonly_inputs=[inp], confine="guard")
        r = ex.run(f"print(open({inp!r}).read())")
        self.assertTrue(r.ok, r.stderr)
        self.assertFalse(r.blocked)
        self.assertIn("payload", r.stdout)

    def test_check_code_unit(self):
        self.assertEqual(check_code("print(1)", self.ws, []), [])
        self.assertTrue(check_code("shutil.rmtree('/x')", self.ws, []))
        self.assertTrue(check_code("import os; os.unlink('/data/x')", self.ws, ["/data/x"]))


if __name__ == "__main__":
    unittest.main()
