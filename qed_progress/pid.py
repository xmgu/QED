import os
import time


def read_pid(output_dir: str):
    path = os.path.join(output_dir, ".qed_ui", "pid")
    # The writer replaces the file atomically. On Windows a reader that lands
    # inside the rename window gets a transient PermissionError; returning None
    # there would show a live run as DEAD for one poll, so retry briefly.
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return None
        except OSError:
            if attempt == 2:
                return None
            time.sleep(0.01)
        except Exception:
            return None
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
