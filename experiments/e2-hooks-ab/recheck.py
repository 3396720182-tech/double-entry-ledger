#!/usr/bin/env python3
"""判定口径改动后，对已有的运行目录重新判定（保留 arm/rep/rc/wall_s）。用法：python3 recheck.py <轮次目录>..."""
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
for rd in sys.argv[1:]:
    for run in sorted(os.listdir(rd)):
        b = os.path.join(rd, run)
        if not os.path.exists(os.path.join(b, "check.json")):
            continue
        old = json.load(open(os.path.join(b, "check.json")))
        tools = os.path.join(b, "orig_tools.json")
        args = [sys.executable, os.path.join(HERE, "check_relprep.py"), os.path.join(b, "proj"), os.path.join(b, "stream.jsonl")]
        if os.path.exists(tools):
            args.append(tools)
        new = json.loads(subprocess.run(args, capture_output=True, text=True).stdout)
        new.update({k: old[k] for k in ("arm", "rep", "rc", "wall_s") if k in old})
        json.dump(new, open(os.path.join(b, "check.json"), "w"), ensure_ascii=False, indent=2)
