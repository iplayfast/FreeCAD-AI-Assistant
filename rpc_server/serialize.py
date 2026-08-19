import FreeCAD as App
import json


def _get_optional_app_type(name: str) -> type | tuple[type, ...] | None:
    value = getattr(App, name, None)
    if isinstance(value, type):
        return value
    if isinstance(value, tuple) and all(isinstance(item, type) for item in value):
        return value
    return None


_COLOR_TYPE = _get_optional_app_type("Color")


def serialize_value(value):
    if isinstance(value, (int, float, str, bool)):
        return value
    elif isinstance(value, App.Vector):
        return {"x": value.x, "y": value.y, "z": value.z}
    elif isinstance(value, App.Rotation):
        return {
            "Axis": {"x": value.Axis.x, "y": value.Axis.y, "z": value.Axis.z},
            "Angle": value.Angle,
        }
    elif isinstance(value, App.Placement):
        return {
            "Base": serialize_value(value.Base),
            "Rotation": serialize_value(value.Rotation),
        }
    elif isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    elif _COLOR_TYPE is not None and isinstance(value, _COLOR_TYPE):
        return tuple(value)
    else:
        return str(value)


def serialize_shape(shape):
    if shape is None:
        return None
    return {
        "Volume": shape.Volume,
        "Area": shape.Area,
        "VertexCount": len(shape.Vertexes),
        "EdgeCount": len(shape.Edges),
        "FaceCount": len(shape.Faces),
    }


def serialize_view_object(view):
    if view is None:
        return None
    result = {}
    try:
        result["ShapeColor"] = serialize_value(view.ShapeColor)
    except AttributeError:
        pass
    try:
        result["Transparency"] = view.Transparency
    except AttributeError:
        pass
    try:
        result["Visibility"] = view.Visibility
    except AttributeError:
        pass
    return result


def _safe(read):
    """Read one field, reporting failure the way the property loop already does.

    ``getattr(obj, "Shape", None)`` only absorbs AttributeError, but a half-built
    object raises RuntimeError("shape is invalid") — which used to fail the whole
    enumeration and make a single bad object hide every other object in the
    document. One unreadable field should cost that field, nothing more.
    """
    try:
        return read()
    except Exception as e:
        return f"<error: {e}>"


def serialize_object(obj):
    if isinstance(obj, list):
        return [serialize_object(item) for item in obj]
    elif isinstance(obj, App.Document):
        return {
            "Name": obj.Name,
            "Label": obj.Label,
            "FileName": obj.FileName,
            "Objects": [serialize_object(child) for child in obj.Objects],
        }
    else:
        result = {
            "Name": obj.Name,
            "Label": obj.Label,
            "TypeId": obj.TypeId,
            "Properties": {},
            "Placement": _safe(lambda: serialize_value(getattr(obj, "Placement", None))),
            "Shape": _safe(lambda: serialize_shape(getattr(obj, "Shape", None))),
            "ViewObject": {},
        }

        for prop in obj.PropertiesList:
            try:
                result["Properties"][prop] = serialize_value(getattr(obj, prop))
            except Exception as e:
                result["Properties"][prop] = f"<error: {str(e)}>"

        if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
            view = obj.ViewObject
            result["ViewObject"] = serialize_view_object(view)

        return result


def demo() -> None:
    """Self-check: an unreadable field must not fail the whole object.

    Needs FreeCAD. From the Python console:
      from rpc_server.serialize import demo; demo()
    """

    class _Broken:
        """Mimics a half-built object: .Shape raises the way FreeCAD's does."""
        Name = "Cube"
        Label = "Cube"
        TypeId = "Part::Box"
        PropertiesList = ["Length", "Bad"]
        Length = 10
        ViewObject = None

        @property
        def Shape(self):
            raise RuntimeError("shape is invalid")

        @property
        def Placement(self):
            raise RuntimeError("no placement")

        @property
        def Bad(self):
            raise RuntimeError("unreadable property")

    out = serialize_object(_Broken())          # must not raise
    assert out["Name"] == "Cube" and out["TypeId"] == "Part::Box", out
    assert "<error: shape is invalid>" == out["Shape"], out["Shape"]
    assert out["Placement"].startswith("<error:"), out["Placement"]
    # Readable siblings still come through — that is the whole point.
    assert out["Properties"]["Length"] == 10, out["Properties"]
    assert out["Properties"]["Bad"].startswith("<error:"), out["Properties"]

    # A list containing one broken object still yields every entry.
    both = serialize_object([_Broken(), _Broken()])
    assert len(both) == 2, both

    print("serialize demo: all checks passed")
