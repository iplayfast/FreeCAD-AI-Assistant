#!/usr/bin/env python3
"""
BIM Floorplan Generator for FreeCAD
Generates a parametric BIM floorplan based on dimensions.txt.
Uses a single, fully-constrained sketch for wall baselines.
"""

import FreeCAD as App
import Part
import Sketcher
import Arch
import Draft

def build_floorplan():
    # 1. Create a new BIM document
    doc = App.newDocument("BIMFloorplan")
    
    # 2. Structural Hierarchy (Site -> Building -> Floor)
    site = Arch.makeSite(name="ProjectSite")
    site.Label = "Project Site"
    
    building = Arch.makeBuilding(name="OfficeBuilding")
    building.Label = "Office Building"
    site.addObject(building)
    
    floor1 = Arch.makeBuildingPart(name="Floor_1")
    floor1.Label = "Floor 1"
    floor1.Placement = App.Placement(App.Vector(0, 0, 0), App.Rotation(0, 0, 0))
    building.addObject(floor1)
    
    # 3. Create Sketch for Wall Baselines
    sketch = doc.addObject("Sketcher::SketchObject", "FloorplanSketch")
    # By default, a Sketch object is created on the XY plane.
    
    # Conversion factor from inches to millimeters (FreeCAD internal unit is mm)
    IN_TO_MM = 25.4
    
    # Lines info: (x1, y1, x2, y2, is_vertical, is_horizontal, length, label)
    # Length is set to None for redundant dimensions to avoid over-constraining the sketch.
    lines_info = [
        # LoftWall (0)
        (0, 0, 0, -128.5, True, False, 128.5, "LoftWall"),
        # NorthWallBathroom (1)
        (0, 0, 150.0, 0, False, True, 150.0, "NorthWallBathroom"),
        # SouthWallBathroom Seg 1 (2)
        (0, -128.5, 60.0, -128.5, False, True, 60.0, "SouthWallBathroom_Seg1"),
        # SouthWallBathroom Seg 2 (3)
        (60.0, -128.5, 60.0, -117.5, True, False, 11.0, "SouthWallBathroom_Seg2"),
        # ClosetFront (4)
        (60.0, -117.5, 121.5, -117.5, False, True, 61.5, "ClosetFront"),
        # ClosetWest (5)
        (69.5, -117.5, 69.5, -139.5, True, False, 22.0, "ClosetWest"),
        # ClosetBack (6) - Length is redundant with ClosetFront and ClosetWest offset
        (69.5, -139.5, 121.5, -139.5, False, True, None, "ClosetBack"),
        # ClosetEast (7) - Length is redundant with ClosetWest
        (121.5, -139.5, 121.5, -117.5, True, False, None, "ClosetEast"),
        # ShowerWest (8)
        (121.5, -117.5, 121.5, -153.5, True, False, 36.0, "ShowerWest"),
        # ShowerBack (9)
        (121.5, -153.5, 156.5, -153.5, False, True, 35.0, "ShowerBack"),
        # ShowerEast (10) - Length is redundant with ShowerWest
        (156.5, -153.5, 156.5, -117.5, True, False, None, "ShowerEast"),
        # SouthWallBathroom Seg 4 (11) - Length is redundant with the overall width loop
        (156.5, -117.5, 150.0, -117.5, False, True, None, "SouthWallBathroom_Seg4"),
        # BedroomWall (12) - Length is redundant with LoftWall and Seg 2
        (150.0, -117.5, 150.0, 0, True, False, None, "BedroomWall"),
        # NorthwallBedroom (13)
        (150.0, 0, 395.5, 0, False, True, 245.5, "NorthwallBedroom"),
        # SouthWallBedroom (14)
        (156.5, -153.5, 389.0, -153.5, False, True, 232.5, "SouthWallBedroom"),
        # EastwallBedroom North (15)
        (395.5, 0, 395.5, -128.0, True, False, 128.0, "EastwallBedroom_North"),
        # EastwallBedroom Angled (16)
        (395.5, -128.0, 389.0, -153.5, False, False, None, "EastwallBedroom_Angled"),
    ]
    
    # Add geometry lines to sketch
    for idx, info in enumerate(lines_info):
        x1, y1, x2, y2 = info[0]*IN_TO_MM, info[1]*IN_TO_MM, info[2]*IN_TO_MM, info[3]*IN_TO_MM
        line = Part.LineSegment(App.Vector(x1, y1, 0), App.Vector(x2, y2, 0))
        sketch.addGeometry(line)
        print(f"Added line {idx}: {info[7]} ({x1}, {y1}) -> ({x2}, {y2})")
        
    # Apply coincident constraints to connect endpoints (T-junctions and loops)
    # Rule: point 1 is start, point 2 is end
    # Lock LoftWall (0) start to the Sketch Origin (-1, 1)
    sketch.addConstraint(Sketcher.Constraint("Coincident", -1, 1, 0, 1))
    # Connect LoftWall start to NorthWallBathroom (1) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, 1, 1))
    # Connect LoftWall end to SouthWallBathroom Seg 1 (2) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 2, 2, 1))
    # Connect SouthWallBathroom Seg 1 end to Seg 2 (3) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 2, 2, 3, 1))
    # Connect SouthWallBathroom Seg 2 end to ClosetFront (4) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 3, 2, 4, 1))
    # Constrain ClosetWest (5) start to lie on ClosetFront (4)
    sketch.addConstraint(Sketcher.Constraint("PointOnObject", 5, 1, 4))
    # Connect ClosetWest end to ClosetBack (6) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 5, 2, 6, 1))
    # Connect ClosetBack end to ClosetEast (7) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 6, 2, 7, 1))
    # Connect ClosetEast end to ClosetFront (4) end
    sketch.addConstraint(Sketcher.Constraint("Coincident", 7, 2, 4, 2))
    # Connect ShowerWest (8) start to ClosetFront (4) end
    sketch.addConstraint(Sketcher.Constraint("Coincident", 8, 1, 4, 2))
    # Connect ShowerWest end to ShowerBack (9) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 8, 2, 9, 1))
    # Connect ShowerBack end to ShowerEast (10) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 9, 2, 10, 1))
    # Connect ShowerEast end to SouthWallBathroom Seg 4 (11) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 10, 2, 11, 1))
    # Connect SouthWallBathroom Seg 4 end to BedroomWall (12) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 11, 2, 12, 1))
    # Connect BedroomWall end to NorthWallBathroom (1) end
    sketch.addConstraint(Sketcher.Constraint("Coincident", 12, 2, 1, 2))
    # Connect BedroomWall end to NorthwallBedroom (13) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 12, 2, 13, 1))
    # Connect NorthwallBedroom end to EastwallBedroom North (15) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 13, 2, 15, 1))
    # Connect EastwallBedroom North end to EastwallBedroom Angled (16) start
    sketch.addConstraint(Sketcher.Constraint("Coincident", 15, 2, 16, 1))
    # Connect EastwallBedroom Angled end to SouthWallBedroom (14) end
    sketch.addConstraint(Sketcher.Constraint("Coincident", 16, 2, 14, 2))
    # Connect SouthWallBedroom start to ShowerBack (9) end
    sketch.addConstraint(Sketcher.Constraint("Coincident", 14, 1, 9, 2))
    
    # Apply Horizontal and Vertical constraints
    for idx, info in enumerate(lines_info):
        is_vert, is_horiz = info[4], info[5]
        if is_vert:
            sketch.addConstraint(Sketcher.Constraint("Vertical", idx))
        elif is_horiz:
            sketch.addConstraint(Sketcher.Constraint("Horizontal", idx))
            
    # Apply Distance constraints to define the wall lengths (converting inches to mm).
    # DistanceY/DistanceX are signed (y2−y1 / x2−x1), so derive the value from the
    # actual coordinates rather than the always-positive length field.
    for idx, info in enumerate(lines_info):
        if info[6] is None:
            continue
        x1, y1, x2, y2 = info[0], info[1], info[2], info[3]
        is_vert, is_horiz = info[4], info[5]
        if is_vert:
            sketch.addConstraint(Sketcher.Constraint("DistanceY", idx, 1, idx, 2, (y2 - y1) * IN_TO_MM))
        elif is_horiz:
            sketch.addConstraint(Sketcher.Constraint("DistanceX", idx, 1, idx, 2, (x2 - x1) * IN_TO_MM))
            
    # Add the DistanceX constraint to position the ClosetWest start offset:
    # Set the horizontal distance between ClosetFront (4) start (point 1) and ClosetWest (5) start (point 1) to 9.5"
    sketch.addConstraint(Sketcher.Constraint("DistanceX", 4, 1, 5, 1, 9.5 * IN_TO_MM))
    
    doc.recompute()
    print("Sketch fully constrained and solved successfully.")
    
    # 4. Create 3D Arch Wall using the sketch as the baseline
    # Wall height: 8 feet = 96 inches = 2438.4 mm
    # Wall thickness: 6 inches = 152.4 mm
    wall = Arch.makeWall(sketch, height=96.0 * IN_TO_MM, width=6.0 * IN_TO_MM, align="Center")
    wall.Label = "FloorplanWalls"
    floor1.addObject(wall)
    
    # 5. Helper function to create doors and windows
    def add_door_or_window(width_in, height_in, label, position, rotation_angle=0, is_door=True):
        w = width_in * IN_TO_MM
        h = height_in * IN_TO_MM
        
        # Profile outline (closed wire in XY plane)
        p1 = App.Vector(0, 0, 0)
        p2 = App.Vector(w, 0, 0)
        p3 = App.Vector(w, h, 0)
        p4 = App.Vector(0, h, 0)
        profile = Draft.makeWire([p1, p2, p3, p4], closed=True)
        
        # Convert to Arch Window object
        window = Arch.makeWindow(profile, name=label)
        window.Label = label
        
        # Placement and rotation
        placement_pos = position
        rotation = App.Rotation(App.Vector(0, 0, 1), rotation_angle)
        window.Placement = App.Placement(placement_pos, rotation)
        
        # Host in the wall (cuts the opening) and add to floor
        Arch.addComponents(window, wall)
        floor1.addObject(window)
        print(f"Added {label} at {position} (rot: {rotation_angle} deg)")
        return window

    # 6. Add doors and windows at exact dimensions from dimensions.txt
    
    # A. LoftWall Door: 41" wide, 80" high, located 6.5" down from origin.
    # LoftWall goes south along Y from Y=0 to Y=-128.5. Door is from Y=-6.5 to Y=-47.5.
    pos_loft_door = App.Vector(0.0 * IN_TO_MM, -47.5 * IN_TO_MM, 0)
    add_door_or_window(41.0, 80.0, "LoftWall_Door", pos_loft_door, rotation_angle=90, is_door=True)
    
    # B. NorthWallBathroom Window: 50" wide, 48" high, located 89" east of origin.
    # Sill height = 36" (3 feet).
    pos_bath_window = App.Vector(89.0 * IN_TO_MM, 0.0 * IN_TO_MM, 36.0 * IN_TO_MM)
    add_door_or_window(50.0, 48.0, "Bath_Window", pos_bath_window, rotation_angle=0, is_door=False)
    
    # C. Closet Door: 40" wide, 80" high, starts 9.5" from closet west corner (starts at X=69.5).
    # Runs along Y = -117.5.
    pos_closet_door = App.Vector(69.5 * IN_TO_MM, -117.5 * IN_TO_MM, 0)
    add_door_or_window(40.0, 80.0, "Closet_Door", pos_closet_door, rotation_angle=0, is_door=True)
    
    # D. Bedroom Door: 32.5" wide, 80" high, starts 4.5" from SouthWallBathroom junction (starts at Y=-113).
    # Runs along X = 150 (vertical wall).
    pos_bedroom_door = App.Vector(150.0 * IN_TO_MM, -113.0 * IN_TO_MM, 0)
    add_door_or_window(32.5, 80.0, "Bedroom_Door", pos_bedroom_door, rotation_angle=90, is_door=True)
    
    # E. Bedroom South Door: 44" wide, 80" high, starts 35" from BedroomWall junction (starts at X=185).
    # Runs along Y = -153.5.
    pos_bed_south_door = App.Vector(185.0 * IN_TO_MM, -153.5 * IN_TO_MM, 0)
    add_door_or_window(44.0, 80.0, "Bedroom_South_Door", pos_bed_south_door, rotation_angle=0, is_door=True)
    
    # F. Bedroom East Window: 51" wide, 48" high, starts at Y=-96 (extends to Y=-45).
    # Runs along X = 395.5 (vertical wall). Sill height = 36".
    pos_bed_east_window = App.Vector(395.5 * IN_TO_MM, -96.0 * IN_TO_MM, 36.0 * IN_TO_MM)
    add_door_or_window(51.0, 48.0, "Bedroom_East_Window", pos_bed_east_window, rotation_angle=90, is_door=False)
    
    doc.recompute()
    
    # 7. Save document to workspace
    import os
    save_path = os.path.join(os.path.expanduser("~"), "BIMProject.FCStd")
    doc.saveAs(save_path)
    print(f"\nSUCCESS: BIM project successfully generated and saved to: {save_path}")

if __name__ == "__main__":
    build_floorplan()
