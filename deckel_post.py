from __future__ import annotations

import argparse
import datetime
import shlex
from dataclasses import dataclass, field
from typing import Dict, Optional

import FreeCAD
import Path
import Path.Post.Utils as PostUtils
import PathScripts.PathUtils as PathUtils
from FreeCAD import Units


class DeckelPostConfig:
    def __init__(self):
        # --- Output control ---
        self.output_comments = False
        self.output_line_numbers = True
        self.output_zero_points = False
        self.show_editor = True
        self.override_rapid_feed = -1

        # --- Formatting ---
        self.precision = 3
        self.modal = False
        self.axis_modal = False
        self.use_tool_length_offset = True

        # --- Units ---
        self.units_gcode = "G21"
        self.unit_format = "mm"
        self.unit_speed_format = "mm/min"

        # --- Program structure ---
        self.preamble = """&P01
D01 +000000
D02 +000000
D03 +000000



%
(&P01/0000)"""
        self.postamble = """?
0000"""
        self.pre_operation = ""
        self.post_operation = """
M30
        """
        self.tool_change = ""

        # --- Machine metadata ---
        self.machine_name = "Deckel FP4A"

        # --- Runtime state ---
        self.line_number = 0
        self.spindle_speed = "+0"
        self.last_spindle_speed = self.spindle_speed

        # --- Timestamp ---
        self.now = datetime.datetime.now()

    def next_line_number(self) -> str:
        if not self.output_line_numbers:
            return ""
        self.line_number += 1
        return f"N{self.line_number:04d} "


class DeckelDialect:
    COMMAND_MAP = {
        "G0": "G00",
        "G1": "G01",
        "G2": "G02",
        "G3": "G03",
        "G54": "G54",
    }

    @classmethod
    def translate(cls, name: str) -> Optional[str]:
        if name not in cls.COMMAND_MAP:
            FreeCAD.Console.PrintWarning(f"Unrecognized command {name}, skipping.\n")
            return None
        return cls.COMMAND_MAP[name]


class DeckelPostProcessor:
    PARAMETER_ORDER = [
        "X",
        "Y",
        "Z",
        "I",
        "J",
        "F",
        "S",
        "T",
        "H",
        "D",
        "R",
        "L",
        "P",
        "Q",
    ]

    def __init__(self, config: DeckelPostConfig):
        self.cfg = config
        self.current_position: Dict[str, str] = {}

    def linenumber(self) -> str:
        return self.cfg.next_line_number()

    def format_length(self, value: float) -> str:
        q = Units.Quantity(value, FreeCAD.Units.Length)
        scaled = 100.0 * float(q.getValueAs(self.cfg.unit_format))
        return f"{round(scaled):+d}"

    def format_feed(self, value: float) -> str:
        q = Units.Quantity(value, FreeCAD.Units.Velocity)
        speed = q.getValueAs(self.cfg.unit_speed_format)
        assert speed >= 0.0, "Feed rate cannot be negative"
        return f"{int(speed)}"

    def format_spindle_speed(self, value: float) -> str:
        return f"{int(round(value)):+d}"

    def parse_path(self, pathobj) -> str:
        output = []
        last_command: Optional[str] = None

        for cmd in pathobj.Path.Commands:
            # spindle commands
            if cmd.Name in ("M3", "M4"):
                direction = 1.0 if cmd.Name == "M3" else -1.0
                self.cfg.spindle_speed = self.format_spindle_speed(
                    direction * float(cmd.Parameters.get("S", self.cfg.spindle_speed))
                )
                continue

            if cmd.Name == "G21":
                continue

            if cmd.Name == "G54" and not self.cfg.output_zero_points:
                continue

            if cmd.Name == "G0" and self.cfg.override_rapid_feed > 0:
                rapid_feed = Units.Quantity(
                    float(self.cfg.override_rapid_feed), self.cfg.unit_speed_format
                )
                cmd.Parameters["F"] = rapid_feed.getValueAs(FreeCAD.Units.Velocity)
                cmd.Name = "G1"

            command = DeckelDialect.translate(cmd.Name)
            if command is None:
                continue

            lines = [[command]]
            parameters_and_values = []

            for p in self.PARAMETER_ORDER:
                if p not in cmd.Parameters:
                    continue

                value = cmd.Parameters[p]

                if p == "F":
                    feed = self.format_feed(value)
                    if self.cfg.modal and (
                        feed == self.current_position.get("F")
                        or cmd.Name
                        in (
                            "G0",
                            "G00",
                        )
                    ):
                        continue
                    parameters_and_values.append((p, feed))
                elif p in ("T", "H", "D"):
                    print(f"Warning: Unhandled parameter {p} with value {value}")
                    # parameters_and_values.append((p, int(value)))
                elif p == "S":
                    self.cfg.spindle_speed = self.format_spindle_speed(float(value))
                elif p in ("X", "Y", "Z"):
                    if self.cfg.axis_modal and self.current_position.get(
                        p
                    ) == self.format_length(value):
                        continue
                    parameters_and_values.append((p, self.format_length(value)))
                else:
                    parameters_and_values.append((p, self.format_length(value)))

            if any(p == "Z" for p, _ in parameters_and_values) and any(
                p == "Y" for p, _ in parameters_and_values
            ):
                # move in X-Y plane first, then Z
                y_value = next(v for p, v in parameters_and_values if p == "Y")
                z_value = next(v for p, v in parameters_and_values if p == "Z")
                parameters_and_values.remove(("Z", z_value))
                lines.append([command, f"Z{z_value}"])

                print(
                    f"Info: Line {self.cfg.line_number + 1}: "
                    f"Splitting up {command} with Y-Z movement: "
                    f"Y{y_value} Z{z_value}"
                )

            for p, v in parameters_and_values:
                lines[0].append(f"{p}{v}")

            if command.startswith("G") and (
                (self.cfg.spindle_speed != self.cfg.last_spindle_speed)
                or not self.cfg.modal
            ):
                lines[0].append(f"S{self.cfg.spindle_speed}")

            # Remove movement command if it does not contain any X, Y or Z
            if command in ("G00", "G01", "G02", "G03"):
                if not any(
                    p in word
                    for p in ("X", "Y", "Z")
                    for line in lines
                    for word in line
                ):
                    continue

            self.cfg.last_spindle_speed = self.cfg.spindle_speed
            self.current_position.update(
                {
                    k: self.format_length(cmd.Parameters[k])
                    for k in ("X", "Y", "Z")
                    if k in cmd.Parameters
                }
            )
            self.current_position.update(
                {"F": self.format_feed(cmd.Parameters["F"])}
                if "F" in cmd.Parameters
                else {}
            )

            for line in lines:
                if line:
                    parsed_line = self.linenumber() + " ".join(line)
                    assert ("Z" not in parsed_line) or ("Y" not in parsed_line), (
                        "Deckel FP4A cannot move Z and Y simultaneously."
                    )
                    output.append(parsed_line)

        return "\n".join(output) if output and output[1] else ""


def build_argument_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deckel", add_help=False)

    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--no-line-numbers", action="store_true")
    p.add_argument("--no-show-editor", action="store_true")
    p.add_argument("--include-zero-points", action="store_true")

    p.add_argument("--precision", type=int, default=3)
    p.add_argument("--preamble")
    p.add_argument("--postamble")

    p.add_argument("--inches", action="store_true")
    p.add_argument("--no-modal", action="store_true")
    p.add_argument("--no-axis-modal", action="store_true")
    p.add_argument("--no-tlo", action="store_true")

    p.add_argument("--override-rapid-feed", type=int, default=-1)

    return p


def parse_arguments(argstring: str, cfg: DeckelPostConfig) -> None:
    parser = build_argument_parser()
    args = parser.parse_args(shlex.split(argstring))

    cfg.output_comments = not args.no_comments
    cfg.output_line_numbers = not args.no_line_numbers
    cfg.output_zero_points = args.include_zero_points
    cfg.show_editor = not args.no_show_editor

    cfg.precision = args.precision
    cfg.modal = not args.no_modal
    cfg.axis_modal = not args.no_axis_modal
    cfg.use_tool_length_offset = not args.no_tlo

    cfg.override_rapid_feed = args.override_rapid_feed

    if args.preamble is not None:
        cfg.preamble = args.preamble
    if args.postamble is not None:
        cfg.postamble = args.postamble

    if args.inches:
        cfg.units_gcode = "G20"
        cfg.unit_format = "in"
        cfg.unit_speed_format = "in/min"
        cfg.precision = 4


def export(objectslist, filename, argstring):
    cfg = DeckelPostConfig()
    parse_arguments(argstring, cfg)
    pp = DeckelPostProcessor(cfg)

    gcode = []

    for line in cfg.preamble.splitlines():
        gcode.append(line)

    for obj in objectslist:
        if not hasattr(obj, "Path"):
            continue
        if parsed := pp.parse_path(obj):
            gcode.append(parsed)

    for op in cfg.post_operation.splitlines():
        if not op.strip():
            continue
        gcode.append(pp.linenumber() + op)

    for line in cfg.postamble.splitlines():
        gcode.append(line)

    final = "\n".join(gcode)

    if FreeCAD.GuiUp and cfg.show_editor and len(final) < 100_000:
        dia = PostUtils.GCodeEditorDialog()
        dia.editor.setText(final)
        if dia.exec_():
            final = dia.editor.toPlainText()

    if filename != "-":
        with open(filename, "w") as fh:
            fh.write(final)

    return final
