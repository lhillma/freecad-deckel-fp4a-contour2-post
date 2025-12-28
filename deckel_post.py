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
        self.spindle_speed = 0.0
        self.last_spindle_speed = 0.0

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
        self.current_position: Dict[str, float] = {}

    def linenumber(self) -> str:
        return self.cfg.next_line_number()

    def format_length(self, value: float) -> str:
        q = Units.Quantity(value, FreeCAD.Units.Length)
        scaled = q.getValueAs(self.cfg.unit_format)
        return f"{int(round(scaled * 100)):+d}"

    def format_feed(self, value: float) -> Optional[str]:
        q = Units.Quantity(value, FreeCAD.Units.Velocity)
        speed = q.getValueAs(self.cfg.unit_speed_format)
        return None if speed <= 0 else f"{int(speed)}"

    def parse_path(self, pathobj) -> str:
        output = []
        last_command: Optional[str] = None

        for cmd in pathobj.Path.Commands:
            # spindle commands
            if cmd.Name in ("M3", "M4"):
                direction = 1.0 if cmd.Name == "M3" else -1.0
                self.cfg.spindle_speed = direction * float(
                    cmd.Parameters.get("S", self.cfg.spindle_speed)
                )
                continue

            if cmd.Name == "G21":
                continue

            if cmd.Name == "G54" and not self.cfg.output_zero_points:
                continue

            command = DeckelDialect.translate(cmd.Name)
            if command is None:
                continue

            words = [command]

            if self.cfg.modal and command == last_command:
                words.pop()

            for p in self.PARAMETER_ORDER:
                if p not in cmd.Parameters:
                    continue

                value = cmd.Parameters[p]

                if p == "F":
                    feed = self.format_feed(value)
                    if feed and cmd.Name not in ("G0", "G00"):
                        words.append(f"F{feed}")
                elif p in ("T", "H", "D", "S"):
                    words.append(f"{p}{int(value)}")
                else:
                    if (
                        not self.cfg.axis_modal
                        and self.current_position.get(p) == value
                    ):
                        continue
                    words.append(p + self.format_length(value))

            if command.startswith("G") and (
                self.cfg.spindle_speed != self.cfg.last_spindle_speed
            ):
                sign = "+" if self.cfg.spindle_speed >= 0 else ""
                words.append(f"S{sign}{int(round(self.cfg.spindle_speed))}")
                self.cfg.last_spindle_speed = self.cfg.spindle_speed

            self.current_position.update(cmd.Parameters)
            last_command = command

            if words:
                output.append(self.linenumber() + " ".join(words))

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
    p.add_argument("--modal", action="store_true")
    p.add_argument("--axis-modal", action="store_true")
    p.add_argument("--no-tlo", action="store_true")

    return p


def parse_arguments(argstring: str, cfg: DeckelPostConfig) -> None:
    parser = build_argument_parser()
    args = parser.parse_args(shlex.split(argstring))

    cfg.output_comments = not args.no_comments
    cfg.output_line_numbers = not args.no_line_numbers
    cfg.output_zero_points = args.include_zero_points
    cfg.show_editor = not args.no_show_editor

    cfg.precision = args.precision
    cfg.modal = args.modal
    cfg.axis_modal = args.axis_modal
    cfg.use_tool_length_offset = not args.no_tlo

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
