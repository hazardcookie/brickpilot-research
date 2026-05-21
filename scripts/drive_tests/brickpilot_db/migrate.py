from __future__ import annotations
import argparse, subprocess
from .config import load_config
from .store import DriveStore

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--config"); ap.add_argument("--print-postgres", action="store_true")
    ns = ap.parse_args(); cfg = load_config(ns.config)
    if cfg.is_postgres:
        if ns.print_postgres:
            print((__import__('pathlib').Path(__file__).with_name('schema.sql')).read_text()); return 0
        schema = __import__('pathlib').Path(__file__).with_name('schema.sql')
        subprocess.check_call(["psql", cfg.database_url, "-v", "ON_ERROR_STOP=1", "-f", str(schema)]); return 0
    st = DriveStore(cfg); st.migrate(); st.close(); print("sqlite/offline schema initialized"); return 0
if __name__ == "__main__": raise SystemExit(main())
