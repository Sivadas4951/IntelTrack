"""
apply_fixes.py
--------------
Run once from the project root (IntelTrack\\):   python apply_fixes.py

  1. Registers the stage-5 router (and /api/value-map) in api/main.py.
  2. Renames the visible product name RivalIQ -> IntelTrack.

Safe to run twice. It only touches the files listed below, keeps each file's line endings,
and leaves internal names alone (database file, JSON keys such as "rivaliq", env var names),
so existing data and the evaluation file keep working.
"""

import os

OLD, NEW = "RivalIQ", "IntelTrack"

RENAME = [
    "api/main.py", "dashboard/app.py", "dashboard/api_client.py",
    "dashboard/views/accuracy.py", "mock_sites/server.py",
    "notifications/digest.py", "notifications/channels.py",
    "collector/compliance.py", "collector/scheduler.py",
    "extraction/extractor.py", "change_intel/debounce.py", "evaluate.py",
]


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def patch_main(path="api/main.py"):
    if not os.path.exists(path):
        return print(f"! {path} not found. Run this from the project root.")
    text = read(path)
    if "stage5_router" in text:
        return print("= api/main.py already registers the stage-5 router")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)

    # 1) import, placed just before `app = FastAPI(`
    i_app = next((i for i, l in enumerate(lines) if l.startswith("app = FastAPI(")), None)
    # 2) router include, placed after the add_middleware(...) call
    i_mw = next((i for i, l in enumerate(lines) if l.startswith("app.add_middleware(")), None)
    if i_app is None or i_mw is None:
        return print("! Could not find the anchor lines in api/main.py. Add by hand:\n"
                     "    from api.stage5 import router as stage5_router\n"
                     "    app.include_router(stage5_router)")
    end = i_mw
    if not lines[i_mw].rstrip().endswith(")"):  # multi-line call: go to its closing paren
        end = next(i for i in range(i_mw, len(lines)) if lines[i].strip() == ")")

    lines.insert(end + 1, "app.include_router(stage5_router)  # stage 5 + value map")
    lines.insert(i_app, "from api.stage5 import router as stage5_router" + eol)
    write(path, eol.join(lines))
    print("+ api/main.py: stage-5 router registered")


def rename():
    for path in RENAME:
        if not os.path.exists(path):
            print(f"- skipped (not found): {path}")
            continue
        text = read(path)
        if OLD in text:
            write(path, text.replace(OLD, NEW))
            print(f"+ renamed in {path} ({text.count(OLD)} place(s))")
        else:
            print(f"= nothing to rename in {path}")


if __name__ == "__main__":
    patch_main()
    rename()
    print("\nDone. Restart the mock site; uvicorn and Streamlit reload on their own.")