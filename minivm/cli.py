"""Command line interface:  python -m minivm run script.asm [options]"""

import argparse
import os
import sys

from .assembler import assemble
from .heap import Heap
from .vm import VM, UncaughtError, to_str


def _max_rss_kb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        return 0


def print_stats(vm, rss_before, rss_after, out):
    st = vm.heap.stats
    w = out.write
    w("== GC statistics ==\n")
    w("minor collections (copying) : %d  (pause total %.2f ms)\n"
      % (st.minor_collections, st.minor_pause_s * 1e3))
    w("major collections (mark&sweep): %d  (pause total %.2f ms)\n"
      % (st.major_collections, st.major_pause_s * 1e3))
    w("max GC pause                : %.2f ms\n" % (st.max_pause_s * 1e3))
    w("objects allocated (total)   : %d\n" % st.allocated)
    w("objects finalized (released): %d\n" % st.finalized)
    w("reachable objects now       : young=%d old=%d (peak live=%d)\n"
      % (st.young_live, st.old_live, st.peak_objects))
    if rss_after:
        w("max RSS                     : %.1f MB (delta %+.1f MB)\n"
          % (rss_after / 1024.0, (rss_after - rss_before) / 1024.0))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="minivm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="assemble and run a script")
    rp.add_argument("script")
    rp.add_argument("--young-size", type=int, default=1024,
                    help="young generation capacity in objects")
    rp.add_argument("--old-threshold", type=int, default=4096,
                    help="old-gen size that triggers a major collection")
    rp.add_argument("--promote-age", type=int, default=1,
                    help="minor GCs an object survives before promotion")
    rp.add_argument("--no-stats", action="store_true",
                    help="do not print GC statistics after the run")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        with open(args.script, "r", encoding="utf-8") as f:
            program = assemble(f.read())
        heap = Heap(young_size=args.young_size,
                    old_threshold=args.old_threshold,
                    promote_age=args.promote_age)
        vm = VM(program, heap)
        rss_before = _max_rss_kb()
        try:
            result = vm.run()
        except UncaughtError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        if result is not None:
            print("=> %s" % to_str(result))
        # final full collection: remaining objects are exactly the
        # reachable ones -> precise reachability report
        vm.heap.full_collect()
        if not args.no_stats:
            print_stats(vm, rss_before, _max_rss_kb(), sys.stdout)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
