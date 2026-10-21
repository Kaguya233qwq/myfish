import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[3] / "docker-entrypoint.sh"


@unittest.skipIf(os.name == "nt", "Docker 启动脚本测试需要 POSIX shell")
class DockerEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trace = self.root / "trace"
        bootstrap = self.root / "bin" / "myfish"
        runtime = self.root / ".myfish" / "venv" / "bin" / "myfish"
        for path, body in (
            (
                bootstrap,
                'printf "bootstrap:%s\\n" "$*" >> "$TRACE"\nexit "${SYNC_EXIT_CODE:-0}"\n',
            ),
            (
                runtime,
                'printf "runtime:%s\\n" "$*" >> "$TRACE"\nprintf "%s\\n" "$$" > "$RUNTIME_PID"\nprintf "%s\\n" "$@" > "$RUNTIME_ARGS"\n',
            ),
        ):
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
            path.chmod(0o755)
        self.environment = dict(
            os.environ,
            PATH=str(bootstrap.parent) + os.pathsep + os.environ.get("PATH", ""),
            TRACE=str(self.trace),
            RUNTIME_PID=str(self.root / "pid"),
            RUNTIME_ARGS=str(self.root / "args"),
        )

    def run_entrypoint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), *args],
            check=False,
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_sync_precedes_run_and_preserves_arguments(self) -> None:
        result = self.run_entrypoint("myfish", "run", "argument with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.trace.read_text().splitlines(),
            [
                "bootstrap:sync",
                "runtime:run argument with spaces",
            ],
        )
        self.assertEqual(
            (self.root / "args").read_text().splitlines(),
            [
                "run",
                "argument with spaces",
            ],
        )

    def test_sync_failure_prevents_bot_start(self) -> None:
        self.environment["SYNC_EXIT_CODE"] = "42"
        result = self.run_entrypoint("myfish", "run")
        self.assertEqual(result.returncode, 42)
        self.assertEqual(self.trace.read_text().splitlines(), ["bootstrap:sync"])
        self.assertFalse((self.root / "pid").exists())

    def test_explicit_sync_command_does_not_start_bot(self) -> None:
        result = self.run_entrypoint("myfish", "sync", "--upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.trace.read_text().splitlines(), ["bootstrap:sync --upgrade"]
        )

    def test_custom_command_bypasses_sync(self) -> None:
        result = self.run_entrypoint("/bin/sh", "-c", "exit 7")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.trace.exists())

    def test_runtime_replaces_entrypoint_process(self) -> None:
        with subprocess.Popen(
            ["/bin/sh", str(ENTRYPOINT), "myfish", "run"],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(int((self.root / "pid").read_text()), process.pid)


if __name__ == "__main__":
    unittest.main()
