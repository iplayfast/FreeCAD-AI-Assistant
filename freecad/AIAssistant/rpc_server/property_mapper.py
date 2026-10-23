# SPDX-License-Identifier: LGPL-2.1-or-later
"""Property assignment from JSON-friendly dicts onto FreeCAD document objects."""

import json
from dataclasses import dataclass, field
from typing import Any

import FreeCAD


def as_property_dict(properties: Any) -> dict[str, Any]:
    """Coerce a properties argument to a dict, or say why it can't be.

    Models routinely send a nested object as a JSON *string*; the bare
    ``AttributeError: 'str' object has no attribute 'items'`` that used to
    surface told them nothing they could act on. Worse, a string reaching
    object_factory would silently substring-match in ``"Part" in properties``.
    """
    if isinstance(properties, str):
        try:
            properties = json.loads(properties)
        except ValueError:
            raise ValueError(
                f"properties must be a JSON object, got the string {properties[:120]!r}"
            ) from None
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ValueError(
            f"properties must be a JSON object, got {type(properties).__name__}"
        )
    return properties


@dataclass
class Object:
    name: str
    type: str | None = None
    analysis: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # object_factory reads .properties directly, so coerce at construction
        # as well as in set_object_property below.
        self.properties = as_property_dict(self.properties)


# Single-object link property types: a string value names the target object.
# Keyed off the actual property type (getTypeIdOfProperty) rather than a
# hardcoded name list, so every link property resolves — not just a chosen few.
# XLink is the default single-link type for many FreeCAD 1.x objects; a same-doc
# XLink accepts a DocumentObject the same way a plain Link does.
_SINGLE_LINK_TYPES = {
    "App::PropertyLink",
    "App::PropertyLinkChild",
    "App::PropertyLinkGlobal",
    "App::PropertyLinkHidden",
    "App::PropertyXLink",
}


def _to_shape_color(val: Any) -> tuple[float, float, float, float]:
    """Normalise a color to a 4-float RGBA tuple.

    Accepts RGB triples (alpha defaults to 1.0) and RGBA quads, matching what
    FreeCAD's ``ShapeColor`` accepts.
    """
    if not isinstance(val, (list, tuple)) or len(val) not in (3, 4):
        raise ValueError(
            f"ShapeColor must be an RGB or RGBA sequence, got {val!r}."
        )
    r, g, b = (float(val[0]), float(val[1]), float(val[2]))
    a = float(val[3]) if len(val) == 4 else 1.0
    return (r, g, b, a)


def parse_reference_entry(entry: Any) -> tuple[str, Any]:
    """Normalise a single ``References`` entry to ``(object_name, sub_element)``.

    Accepts both the documented dict form
    ``{"object_name": "Box", "face": "Face1"}`` and the legacy
    ``["Box", "Face1"]`` pair form.
    """
    if isinstance(entry, dict):
        ref_name = entry.get("object_name", entry.get("Object"))
        face = entry.get("face", entry.get("Face"))
        if ref_name is None:
            raise ValueError(
                f"Reference entry {entry!r} is missing an 'object_name' key."
            )
        return ref_name, face
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    raise ValueError(
        f"Invalid reference entry {entry!r}; expected "
        "{'object_name': ..., 'face': ...} or [object_name, face]."
    )


def resolve_references(doc: FreeCAD.Document, val: Any) -> list[tuple[Any, Any]]:
    """Resolve a ``References`` list into ``(DocumentObject, sub_element)`` tuples."""
    refs = []
    for entry in val:
        ref_name, face = parse_reference_entry(entry)
        ref_obj = doc.getObject(ref_name)
        if ref_obj is None:
            raise ValueError(f"Referenced object '{ref_name}' not found.")
        refs.append((ref_obj, face))
    return refs


def set_object_property(
    doc: FreeCAD.Document, obj: FreeCAD.DocumentObject, properties: dict[str, Any]
):
    failures = []
    for prop, val in as_property_dict(properties).items():
        try:
            if prop in obj.PropertiesList:
                if prop == "Placement" and isinstance(val, dict):
                    if "Base" in val:
                        pos = val["Base"]
                    elif "Position" in val:
                        pos = val["Position"]
                    else:
                        pos = {}
                    rot = val.get("Rotation", {})
                    placement = FreeCAD.Placement(
                        FreeCAD.Vector(
                            pos.get("x", 0),
                            pos.get("y", 0),
                            pos.get("z", 0),
                        ),
                        FreeCAD.Rotation(
                            FreeCAD.Vector(
                                rot.get("Axis", {}).get("x", 0),
                                rot.get("Axis", {}).get("y", 0),
                                rot.get("Axis", {}).get("z", 1),
                            ),
                            rot.get("Angle", 0),
                        ),
                    )
                    setattr(obj, prop, placement)

                elif isinstance(getattr(obj, prop), FreeCAD.Vector) and isinstance(
                    val, dict
                ):
                    vector = FreeCAD.Vector(
                        val.get("x", 0), val.get("y", 0), val.get("z", 0)
                    )
                    setattr(obj, prop, vector)

                elif (
                    isinstance(val, str)
                    and obj.getTypeIdOfProperty(prop) in _SINGLE_LINK_TYPES
                ):
                    ref_obj = doc.getObject(val)
                    if ref_obj is None:
                        raise ValueError(f"Referenced object '{val}' not found.")
                    setattr(obj, prop, ref_obj)

                elif prop == "References" and isinstance(val, list):
                    setattr(obj, prop, resolve_references(doc, val))

                else:
                    setattr(obj, prop, val)
            # ShapeColor is a property of the ViewObject
            elif prop == "ShapeColor" and isinstance(val, (list, tuple)):
                setattr(obj.ViewObject, prop, _to_shape_color(val))

            elif prop == "ViewObject" and isinstance(val, dict):
                for k, v in val.items():
                    if k == "ShapeColor":
                        setattr(obj.ViewObject, k, _to_shape_color(v))
                    else:
                        setattr(obj.ViewObject, k, v)

            else:
                setattr(obj, prop, val)

        except Exception as e:
            FreeCAD.Console.PrintError(f"Property '{prop}' assignment error: {e}\n")
            failures.append(f"{prop}: {e}")

    if failures:
        raise ValueError(
            "Failed to set propert" + ("y" if len(failures) == 1 else "ies")
            + ": " + "; ".join(failures)
        )


def demo() -> None:
    """Self-check of the properties coercion.

    Needs FreeCAD. From the Python console:
      from freecad.AIAssistant.rpc_server.property_mapper import demo; demo()
    """
    # A JSON object string is accepted — models send these routinely.
    assert as_property_dict('{"Length": 10}') == {"Length": 10}
    assert as_property_dict({"Length": 10}) == {"Length": 10}
    assert as_property_dict(None) == {}
    assert as_property_dict({}) == {}

    # Anything that cannot be a property map is rejected by name, not by
    # AttributeError from .items() three frames deeper.
    for bad in ("not json", "", "[1, 2]", 42, ["a"]):
        try:
            as_property_dict(bad)
        except ValueError as e:
            assert "properties must be a JSON object" in str(e), e
        else:
            raise AssertionError(f"should have rejected {bad!r}")

    # Object coerces at construction, so object_factory's direct reads of
    # .properties see a dict — never a string that would substring-match.
    assert Object(name="Cube", properties='{"Part": "Box"}').properties == {"Part": "Box"}
    assert Object(name="Cube").properties == {}
    assert "Part" in Object(name="C", properties='{"Part": "Box"}').properties

    print("property_mapper demo: all checks passed")
