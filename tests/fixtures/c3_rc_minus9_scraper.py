"""[wave-33 C3 deliverable] rc=-9 fixture scraper.

Spawns an ESCAPED grandchild (its own session — the cloak/detached shape),
prints its pid (``GRANDCHILD <pid>``), then SIGKILLs its own process group
so the runner observes returncode -9 (group leader killed) while the
grandchild survives, reparented to init — the exact orphan a BFS-from-dead-
parent killer can never reach; only the C3 sweep does.

The grandchild binary is a copy of this interpreter named ``chromium-
fixture-bin`` so the sweep's chrome|chromium cmdline pattern matches it
(the way the real cloak Chromium would).
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="c3fixture_")
    fake_chrome = os.path.join(tmp, "chromium-fixture-bin")
    shutil.copy(sys.executable, fake_chrome)
    grandchild = subprocess.Popen(
        [fake_chrome, "-c", "import time; time.sleep(90)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # escape: own session, survives the group kill
    )
    print(f"GRANDCHILD {grandchild.pid}", flush=True)
    time.sleep(0.5)  # grandchild is born, execed, reparent-eligible
    os.killpg(os.getpgid(0), signal.SIGKILL)  # group suicide → runner sees -9
    # unreachable


if __name__ == "__main__":
    main()
