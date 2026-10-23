# SPDX-License-Identifier: LGPL-2.1-or-later
"""Self-check for the two defects the bearing run exposed. Run:

    freecadcmd test_crossbody.py

Both were silent: the wrong thing happened and the call reported success, so
the model built the rest of the model on top of it.
"""

import os
import sys

import FreeCAD

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
for _stale in [m for m in list(sys.modules)
               if m == "freecad.AIAssistant" or m.startswith("freecad.AIAssistant.")]:
    del sys.modules[_stale]

import freecad.AIAssistant.rpc_server as _pkg

if not os.path.abspath(_pkg.__file__).startswith(_HERE + os.sep):
    raise SystemExit(f"WRONG COPY: rpc_server is {_pkg.__file__}, not {_HERE}")

from freecad.AIAssistant.rpc_server.tools import call_tool


def out(line: str) -> None:
    """FreeCAD swallows plain print() from a freecadcmd script; Console does not."""
    FreeCAD.Console.PrintMessage(line + "\n")


def main() -> int:
    doc = FreeCAD.newDocument("_crossbody_demo")
    failures = []

    def check(label, fn):
        try:
            fn()
            out(f"  ok   {label}")
        except AssertionError as exc:
            failures.append(f"{label}: {exc}")
            out(f"  FAIL {label}: {exc}")

    try:
        call_tool("create_body", {"doc_name": doc.Name, "name": "InnerRing"})
        call_tool("create_body", {"doc_name": doc.Name, "name": "OuterRing"})
        inner_sk = call_tool("create_sketch", {
            "doc_name": doc.Name, "body_name": "InnerRing", "name": "Base"})["sketch_name"]
        call_tool("sketch_circle", {
            "doc_name": doc.Name, "sketch_name": inner_sk, "radius": 4})

        # 1. A sketch belonging to InnerRing must not pad into OuterRing.
        #    This used to succeed and report volume 351.858.
        def cross_body_pad():
            try:
                got = call_tool("pad", {"doc_name": doc.Name, "body_name": "OuterRing",
                                        "sketch_name": inner_sk, "length": 7})
            except ValueError as exc:
                assert "belongs to body 'InnerRing'" in str(exc), exc
                return
            raise AssertionError(f"padded another body's sketch and returned {got}")

        check("pad refuses a sketch owned by a different body", cross_body_pad)

        # The same sketch into its OWN body still works — the guard must not
        # turn a correct call into a failure.
        def same_body_pad():
            got = call_tool("pad", {"doc_name": doc.Name, "body_name": "InnerRing",
                                    "sketch_name": inner_sk, "length": 7})
            assert got["failed"] is False, got
            assert got["solids"] == 1, got
            assert got["volume"] > 0, got

        check("pad still works within one body", same_body_pad)

        # 2. create_object must return the object whose TYPE was requested. A
        #    Body drags an Origin and six origin features in with it, and the
        #    old added[-1] fallback handed back Origin-Point005.
        def body_identity():
            call_tool("create_object", {"doc_name": doc.Name,
                                        "obj_type": "PartDesign::Body", "obj_name": "Race"})
            again = call_tool("create_object", {"doc_name": doc.Name,
                                                "obj_type": "PartDesign::Body", "obj_name": "Race"})
            made = doc.getObject(again["object_name"])
            assert made is not None, again
            assert made.TypeId == "PartDesign::Body", f"got {made.TypeId} ({again})"
            assert again.get("renamed_from") == "Race", again

        check("create_object returns the Body, not its origin child", body_identity)

        # Plain objects create nothing extra; the uniquify report must survive.
        def plain_identity():
            call_tool("create_object", {"doc_name": doc.Name,
                                        "obj_type": "Part::Box", "obj_name": "Blk"})
            again = call_tool("create_object", {"doc_name": doc.Name,
                                                "obj_type": "Part::Box", "obj_name": "Blk"})
            assert again["object_name"] == "Blk001", again
            assert again["renamed_from"] == "Blk", again

        check("create_object still reports the uniquified name", plain_identity)
    finally:
        FreeCAD.closeDocument(doc.Name)

    if failures:
        out(f"\n{len(failures)} FAILED")
        return 1
    out("\ntest_crossbody: all checks passed")
    return 0


sys.exit(main())
