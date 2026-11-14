import os


def read_pid(output_dir: str):
    try:
        with open(os.path.join(output_dir, ".qed_ui", "pid")) as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False