# SPDX-License-Identifier: LGPL-2.1-or-later
# Nothing to do at non-GUI startup. (This used to call
# FreeCAD.addImportType("AI Workbench", "AI"), which advertised the workbench as
# a file format handler; the module has no open()/insert(), so it could only
# fail if FreeCAD ever took it up on the offer.)
