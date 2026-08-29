#!/usr/bin/env python3
"""Start the Confident Planner web app:  python run.py  [--port 5000]"""
import argparse
import logging

from frontend.app import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    create_app().run(host=args.host, port=args.port, debug=args.debug)
