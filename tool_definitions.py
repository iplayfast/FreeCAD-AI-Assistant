TOOLS = [
    {
        "name": "execute_python",
        "description": (
            "Execute Python code in FreeCAD's live environment. "
            "Has access to FreeCAD (as App), FreeCADGui (as Gui), Part, Sketcher, "
            "Draft, Arch, PartDesign, and all installed workbench modules. "
            "stdout is captured and returned. Always call App.ActiveDocument.recompute() "
            "after creating or modifying objects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute in FreeCAD's interpreter. "
                        "Variables persist across calls within the same conversation — "
                        "objects created in one call can be referenced by name in the next. "
                        "Use print() statements to narrate progress; output is captured and returned. "
                        "Hard timeout: 30 seconds."
                    ),
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file from the filesystem and return its contents. "
            "Use this to load dimension specs, survey notes, or any plain-text "
            "reference document before starting a modelling task. "
            "Supports UTF-8 text files up to 64 KB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file, e.g. '/home/chris/Documents/dimensions.txt'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "open_document",
        "description": (
            "Open a FreeCAD file (.FCStd) from disk and make it the active document. "
            "Returns the document name and a list of objects it contains. "
            "Use this to load an existing project before inspecting or modifying it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the .FCStd file, e.g. '/home/chris/Documents/project.FCStd'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "save_document",
        "description": (
            "Save the active FreeCAD document. "
            "If path is provided, saves to that location (saveAs). "
            "If omitted, saves in place using the document's existing filename."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to save to, e.g. '/home/chris/Documents/project.FCStd'. Omit to save in place.",
                }
            },
        },
    },
    {
        "name": "delete_object",
        "description": (
            "Delete an object from the active document by its internal Name or Label. "
            "Use this to remove a failed sketch, wall, or opening before retrying. "
            "Triggers a recompute after deletion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Internal Name or Label of the object to delete (e.g. 'Sketch', 'Wall', 'Door')",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "undo",
        "description": (
            "Undo the last one or more operations in the active document. "
            "Use when an operation produces an unexpected result and you want to revert before retrying."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "integer",
                    "description": "Number of undo steps (default: 1)",
                }
            },
        },
    },
    {
        "name": "export_file",
        "description": (
            "Export objects from the active document to a file. "
            "Format is inferred from the file extension: "
            ".ifc for IFC/BIM, .dxf for AutoCAD, .svg for 2D vector, .stl for mesh. "
            "If object_names is omitted, exports all objects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute export path including extension, e.g. '/home/chris/Documents/floorplan.ifc'",
                },
                "object_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of object Names or Labels to export. Omit to export everything.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_document_info",
        "description": (
            "Return information about the current FreeCAD document: "
            "document name, list of objects (name + type), current selection, "
            "and active workbench."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_workbenches",
        "description": "Return a list of all installed FreeCAD workbench names.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "switch_workbench",
        "description": "Activate a FreeCAD workbench by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workbench name, e.g. 'BIM', 'Sketcher', 'Part', 'Draft'",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_commands",
        "description": (
            "List FreeCAD commands available in the given workbench. "
            "If workbench is omitted, lists commands in the active workbench."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workbench": {
                    "type": "string",
                    "description": "Workbench name (optional)",
                }
            },
        },
    },
    {
        "name": "fit_view",
        "description": "Fit all objects into the 3D viewport.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recompute",
        "description": "Trigger a recompute of the active FreeCAD document.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sketch_geometry",
        "description": (
            "Return every line and arc in a sketch with its wall label and "
            "start/end coordinates (in mm). Use this to inspect what is already "
            "in a sketch before adding more lines, or to resume after an "
            "interrupted session instead of deleting and starting over. "
            "Labels are extracted from the constraint names set by sketch_line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch_name": {
                    "type": "string",
                    "description": "Internal Name or Label of the sketch to inspect",
                }
            },
            "required": ["sketch_name"],
        },
    },
    {
        "name": "check_sketch",
        "description": (
            "Verify a sketch's constraint status before using it as a wall baseline. "
            "Returns fully_constrained (bool), solver_status, conflicting_constraints, "
            "and redundant_constraints. "
            "ALWAYS call this after constraining a sketch and before calling add_wall_from_sketch. "
            "If conflicting_constraints is non-empty the sketch has sign errors — "
            "fix the DistanceY/DistanceX values before proceeding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch_name": {
                    "type": "string",
                    "description": "Internal Name or Label of the sketch to check",
                }
            },
            "required": ["sketch_name"],
        },
    },
    {
        "name": "create_bim_document",
        "description": (
            "Create a new FreeCAD document with a standard BIM hierarchy: "
            "Site → Building → Floor (BuildingPart). "
            "Returns labels of the created objects for use in subsequent calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Document name (default: 'BIMProject')",
                }
            },
        },
    },
    {
        "name": "add_wall_from_sketch",
        "description": (
            "Create an Arch Wall from an existing Sketcher sketch and add it to a floor. "
            "The sketch must already exist in the active document. "
            "Call get_document_info first to find the sketch's object Name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch_name": {
                    "type": "string",
                    "description": "Internal object Name of the sketch (e.g. 'FloorplanSketch')",
                },
                "height_mm": {
                    "type": "number",
                    "description": "Wall height in millimetres",
                },
                "width_mm": {
                    "type": "number",
                    "description": "Wall thickness in millimetres",
                },
                "floor_label": {
                    "type": "string",
                    "description": "Label of the BuildingPart (floor) to add the wall to (optional)",
                },
                "align": {
                    "type": "string",
                    "description": "'Center', 'Left', or 'Right' (default: 'Center')",
                },
            },
            "required": ["sketch_name", "height_mm", "width_mm"],
        },
    },
    {
        "name": "add_opening",
        "description": (
            "Create a door or window and cut it into an existing Arch Wall. "
            "pos_x and pos_y are the absolute X/Y position in mm on the wall face. "
            "sill_mm is the height above the floor (Z offset). "
            "rotation_deg rotates around the Z axis (90 for walls running along Y). "
            "Returns the opening's label."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "wall_label": {
                    "type": "string",
                    "description": "Label of the target Arch Wall",
                },
                "width_mm":  {"type": "number", "description": "Opening width in mm"},
                "height_mm": {"type": "number", "description": "Opening height in mm"},
                "pos_x":     {"type": "number", "description": "Absolute X position in mm"},
                "pos_y":     {"type": "number", "description": "Absolute Y position in mm"},
                "sill_mm":   {"type": "number", "description": "Sill/floor height in mm (default 0)"},
                "rotation_deg": {"type": "number", "description": "Z rotation in degrees (default 0)"},
                "label":     {"type": "string", "description": "Name for the opening object"},
                "is_door":   {"type": "boolean", "description": "True for door, False for window"},
            },
            "required": ["wall_label", "width_mm", "height_mm", "pos_x", "pos_y", "label"],
        },
    },
    {
        "name": "get_skill",
        "description": (
            "Retrieve detailed documentation, step-by-step workflows, and Python "
            "code recipes for a specific FreeCAD module/workbench. Use this when "
            "you need reference instructions on how to use functions from modules "
            "like BIM/Arch, Draft, Sketcher, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the skill/workbench to look up (e.g. 'bim')",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_wiki",
        "description": (
            "Search the local FreeCAD wiki documentation mirror for a command, constraint, "
            "or module topic. Returns the Description and Scripting sections of the best "
            "matching page, including Python API examples. "
            "Use this before writing code for any FreeCAD command you are unsure about — "
            "especially Sketcher constraints, Arch/BIM tools, Draft commands, and PartDesign features. "
            "Examples: 'Sketcher_ConstrainDistance', 'Arch_Wall', 'Draft_Line', 'PartDesign_Pad'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Command or topic to look up. Use the FreeCAD wiki page name format "
                        "when known (e.g. 'Sketcher_ConstrainDistanceX', 'Arch_Window', "
                        "'BIM_Door'), or a plain search term (e.g. 'distance constraint', "
                        "'wall opening')."
                    ),
                }
            },
            "required": ["query"],
        },
    },
]
