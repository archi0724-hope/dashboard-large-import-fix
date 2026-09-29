"""Command line interface:  python -m app <command>

    demo        generate synthetic sample files, run everything on them and export (safe first run)
    serve       start the dashboard + API (default http://127.0.0.1:8000)
    scan        list the files in the configured source folder
    inspect     show sheets / header rows / column mapping per file (no records written)
    dry-run     run a small sample and print the proposed standardisation
    run         run the full pipeline (resumable)
    export      write the output files
    reviews     list pending manual reviews
    drive-auth  one-time Google Drive authorisation (read-only)
    reset       clear derived results (add --all to also clear raw data and decisions)
    benchmark   time the pipeline on N synthetic records
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from .config import ConfigError
from .services import AppState


def _state(args) -> AppState:
    st = AppState()
    upd = {}
    if getattr(args, "input", None):
        upd.update(source_kind="local", local_input_dir=args.input)
    if getattr(args, "drive", None):
        upd.update(source_kind="drive", drive_folder=args.drive)
    if getattr(args, "output", None):
        upd["output_dir"] = args.output
    if getattr(args, "entity_type", None):
        upd["entity_type"] = args.entity_type
    if upd:
        st.update_config(upd)
    return st


def _print_progress(st: AppState) -> None:
    last = ""
    while st._thread and st._thread.is_alive():
        p = st.progress.snapshot()
        line = f"{p['percent']:5.1f}%  {p['message']}"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(0.5)
    st.wait()


def _report(st: AppState) -> int:
    p = st.progress.snapshot()
    if p["error"]:
        print(f"\nStopped: {p['error']}", file=sys.stderr)
        return 1
    t = st.pipeline().totals()
    print("\nDone. " + ", ".join(f"{k}={v}" for k, v in t.items()))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    s = AppState().settings
    print(f"Dashboard: http://{args.host or s.dashboard_host}:{args.port or s.dashboard_port}")
    uvicorn.run("app.main:app", host=args.host or s.dashboard_host, port=args.port or s.dashboard_port, log_level="warning")
    return 0


def cmd_demo(args) -> int:
    st = AppState()
    print("Generating synthetic demo files ...")
    print(json.dumps(st.prepare_demo()))
    st.reset("all")
    st.update_config({"ocr_enabled": True})
    st.start_run()
    _print_progress(st)
    rc = _report(st)
    if rc == 0:
        files = st.exporter().export_all()
        print(f"\n{len(files)} output files written to {st.exporter().out}")
        print("Next: `python -m app serve` and open the dashboard to review the uncertain matches.")
    return rc


def cmd_scan(args) -> int:
    st = _state(args)
    pipe = st.pipeline()
    pipe.discover()
    df = pd.DataFrame(pipe.inventory())
    print(df[["source_path", "ext", "size_bytes", "status"]].to_string(index=False))
    return 0


def cmd_inspect(args) -> int:
    st = _state(args)
    for f in st.pipeline().inspect():
        print(f"\n{f['path']}  [{f['status']}]  records={f.get('records')}")
        for b in f.get("blocks", []):
            loc = b.get("sheet") or (f"page {b['page']}" if b.get("page") else "-")
            print(f"  {loc}: header row {b.get('header_row')} ({b.get('header_source')}), {b.get('records')} records")
            for c in b.get("columns", []):
                print(f"     {c['original']!r:32} -> {c['field'] or '(kept, unmapped)'}  [{c['method']}]")
        for i in f.get("issues", []):
            print(f"  ! {i['severity']}: {i['message']}")
    return 0


def cmd_dry_run(args) -> int:
    st = _state(args)
    df = st.dry_run(args.sample)
    pd.set_option("display.width", 220, "display.max_colwidth", 40)
    print(df[["file", "row", "original_name", "suggested_standard_name", "confidence", "status", "reason"]].to_string(index=False))
    return 0


def cmd_run(args) -> int:
    st = _state(args)
    st.start_run(args.limit_per_file)
    try:
        _print_progress(st)
    except KeyboardInterrupt:
        print("Stopping (progress is saved) ...")
        st.stop_run()
        st.wait()
    rc = _report(st)
    if rc == 0 and args.export:
        print(f"{len(st.exporter().export_all())} output files written")
    return rc


def cmd_export(args) -> int:
    st = _state(args)
    for m in st.exporter().export_all():
        print(f"{m['file']:55} {m['rows']:>8} rows  {m['note']}")
    return 0


def cmd_reviews(args) -> int:
    st = _state(args)
    res = st.reviews().list_items(limit=args.limit)
    print(f"{res['total']} pending")
    for i in res["items"]:
        print(f"#{i['review_id']:<4} {i['match_score']:>5}  {i['original_name']!r} ({i['context_city'] or '-'}) ~ "
              f"{(i['candidate'] or {}).get('standard_name')!r}  [{i['record_count']} records]  {i['reason']}")
    return 0


def cmd_drive_auth(args) -> int:
    from .drive.google_drive import run_oauth_flow

    print("Authorised. Token stored at", run_oauth_flow(AppState().settings))
    return 0


def cmd_reset(args) -> int:
    AppState().reset("all" if args.all else "derived")
    print("Reset done.")
    return 0


def cmd_benchmark(args) -> int:
    import tempfile

    from scripts.generate_sample_data import make_large

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in"
        src.mkdir()
        make_large(src / "large.csv", args.records, max(50, args.records // 8))
        st = AppState(db_path=Path(d) / "bench.duckdb")
        st.update_config({"source_kind": "local", "local_input_dir": str(src), "output_dir": str(Path(d) / "out")})
        t0 = time.time()
        st.start_run()
        _print_progress(st)
        secs = time.time() - t0
        t = st.pipeline().totals()
        print(f"\n{args.records:,} records -> {t['entities']:,} entities, {t['pending_reviews']:,} reviews in {secs:.1f}s "
              f"({args.records / max(secs, 0.001):,.0f} records/s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--input", help="local input folder (overrides the saved configuration)")
        p.add_argument("--drive", help="Google Drive folder URL or id")
        p.add_argument("--output", help="output folder")
        p.add_argument("--entity-type", choices=["hospital", "company", "person", "generic"])

    p = sub.add_parser("serve"); p.add_argument("--host"); p.add_argument("--port", type=int); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("demo"); p.set_defaults(fn=cmd_demo)
    for name, fn in (("scan", cmd_scan), ("inspect", cmd_inspect), ("export", cmd_export)):
        p = sub.add_parser(name); common(p); p.set_defaults(fn=fn)
    p = sub.add_parser("dry-run"); common(p); p.add_argument("--sample", type=int, default=60); p.set_defaults(fn=cmd_dry_run)
    p = sub.add_parser("run"); common(p); p.add_argument("--limit-per-file", type=int); p.add_argument("--export", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("reviews"); common(p); p.add_argument("--limit", type=int, default=30); p.set_defaults(fn=cmd_reviews)
    p = sub.add_parser("drive-auth"); p.set_defaults(fn=cmd_drive_auth)
    p = sub.add_parser("reset"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_reset)
    p = sub.add_parser("benchmark"); p.add_argument("--records", type=int, default=20000); p.set_defaults(fn=cmd_benchmark)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
