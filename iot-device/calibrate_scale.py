#!/usr/bin/env python3
"""
Calibrate one HX711 load cell and (optionally) save the result into config.json.

  python3 calibrate_scale.py --name waste_bin          pins + result read from/saved to config.json
  python3 calibrate_scale.py --dout 5 --sck 6          just print the numbers

Steps:
  1. Put the EMPTY container on the scale (the bin, the rice drum ...) -> Enter.
     Its weight becomes zero ("offset").
  2. Put something whose weight you know (a 1 kg / 5 kg packet works) -> type its weight in kg.
     This gives the "scale" (raw counts per kg).
  3. Watch live readings to check, Ctrl+C to finish.
"""

import argparse
import json
import os
import shutil
import sys
import time

from hx711 import HX711

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--name", help="scale name in config.json (pins are read from there, result saved there)")
    p.add_argument("--dout", type=int)
    p.add_argument("--sck", type=int)
    p.add_argument("--samples", type=int, default=15)
    args = p.parse_args()

    cfg, entry = None, None
    if args.name:
        with open(args.config) as f:
            cfg = json.load(f)
        entry = next((s for s in cfg.get("scales", []) if s.get("name") == args.name), None)
        if entry is None:
            sys.exit(f"No scale named '{args.name}' in {args.config}")
        args.dout = args.dout or entry["dout_pin"]
        args.sck = args.sck or entry["sck_pin"]
    if args.dout is None or args.sck is None:
        sys.exit("Give --name, or both --dout and --sck (BCM pin numbers)")

    hx = HX711(args.dout, args.sck)
    try:
        input(f"\n[1/2] Put the EMPTY container on the scale (DT=GPIO{args.dout}, SCK=GPIO{args.sck}) and press Enter ")
        offset = hx.read_median(args.samples)
        print(f"      offset = {offset:.0f}")

        while True:
            known = input("[2/2] Put a known weight on it and type that weight in kg (e.g. 5): ").strip()
            try:
                known_kg = float(known)
                if known_kg > 0:
                    break
            except ValueError:
                pass
            print("      Please type a number greater than 0")
        raw = hx.read_median(args.samples)
        scale = (raw - offset) / known_kg
        if abs(scale) < 1:
            sys.exit("      The reading barely changed: check the load-cell wiring and try again.")
        print(f"      scale = {scale:.2f} counts per kg")

        print('\nValues for config.json:\n  "offset": %.0f,\n  "scale": %.2f\n' % (offset, scale))

        if entry is not None:
            entry["offset"] = round(offset)
            entry["scale"] = round(scale, 2)
            shutil.copyfile(args.config, args.config + ".bak")
            with open(args.config, "w") as f:
                json.dump(cfg, f, indent=2)
            print(f"Saved into {args.config} (previous version kept as config.json.bak)\n")

        print("Live readings (Ctrl+C to stop):")
        while True:
            kg = (hx.read_median(5) - offset) / scale
            print(f"  {kg:8.3f} kg", end="\r", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        HX711.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
