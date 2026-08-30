#!/usr/bin/env python3
"""Start the Confident Planner web app locally:  python run.py  [--port 5000] [--no-debug]

Debug mode is on by default (templates and code reload on edit, tracebacks in the browser).
Production (Cloud Run) does not use this file - gunicorn serves frontend.wsgi:app instead.
"""
import argparse
import logging
import os

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-debug", action="store_true", help="plain server: no auto-reload, no browser debugger")
    args = ap.parse_args()
    debug = not args.no_debug
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        # Reloader parent: werkzeug re-runs this script in a child (WERKZEUG_RUN_MAIN=true) and respawns it whenever
        # the child exits for a reload. Don't build the app here - it would load the 900k-row dataset a second time.
        from werkzeug.serving import run_simple

        run_simple(args.host, args.port, lambda environ, start_response: [], use_reloader=True)
    else:
        from frontend.app import create_app

        # In the child, use_reloader=True makes werkzeug watch this process's imported modules and exit for a restart
        # when one changes; templates reload without a restart because debug sets TEMPLATES_AUTO_RELOAD.
        create_app().run(host=args.host, port=args.port, debug=debug, use_reloader=debug)
