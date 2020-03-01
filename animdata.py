#!/usr/bin/env python
"""Extracts and saves AnimData.D2"""

import argparse
import csv
import itertools
import json
import logging
import struct
import sys
from typing import BinaryIO, Iterable, List, NamedTuple, Optional, TextIO, Tuple


# Logger used by the CLI program
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Error(Exception):
    """Base class for all errors thrown by this module."""


class LoadTxtError(Error):
    """Raised when loading a TXT file fails.

    Attributes:
        row: Row index that caused the failure (starts at 0).
    """

    def __init__(self, message: str, row: Optional[int] = None) -> None:
        super().__init__(message + ("" if row is None else f" (at row index {row})"))
        self.row = row


def hash_cof_name(cof_name: str) -> int:
    """Returns the block hash for the given COF name."""
    # Based on:
    #   https://d2mods.info/forum/viewtopic.php?p=24163#p24163
    #   https://d2mods.info/forum/viewtopic.php?p=24295#p24295
    return sum(map(ord, cof_name.upper())) % 256


class ActionTrigger(NamedTuple):
    """Represents a single action trigger frame in an AnimData record."""

    frame: int
    code: int


class Record(NamedTuple):
    """Represents an AnimData record entry."""

    cof_name: str
    frames_per_direction: int
    animation_speed: int
    triggers: Tuple[ActionTrigger, ...]

    def make_dict(self) -> dict:
        """Returns a plain dict that can be serialized to another format."""
        return {
            "cof_name": self.cof_name,
            "frames_per_direction": self.frames_per_direction,
            "animation_speed": self.animation_speed,
            "triggers": [trigger._asdict() for trigger in self.triggers],
        }

    @classmethod
    def from_dict(cls, obj: dict) -> "Record":
        """Creates a new record from a dict unserialized from another format."""
        cof_name = obj["cof_name"]
        if not isinstance(cof_name, str):
            raise TypeError(f"cof_name must be a string (got {cof_name!r})")

        frames_per_direction = obj["frames_per_direction"]
        if not isinstance(frames_per_direction, int):
            raise TypeError(
                f"frames_per_direction must be an integer "
                f"(got {frames_per_direction!r})"
            )

        animation_speed = obj["animation_speed"]
        if not isinstance(animation_speed, int):
            raise TypeError(
                f"animation_speed must be an integer (got {animation_speed!r})"
            )

        triggers = []
        for trigger_dict in obj["triggers"]:
            trigger = ActionTrigger(**trigger_dict)
            if not isinstance(trigger.frame, int):
                raise TypeError(
                    f"Trigger frame must be an integer (got {trigger.frame!r})"
                )
            if not isinstance(trigger.code, int):
                raise TypeError(
                    f"Trigger code must be an integer (got {trigger.code!r})"
                )
            triggers.append(trigger)

        return cls(
            cof_name=cof_name,
            frames_per_direction=frames_per_direction,
            animation_speed=animation_speed,
            triggers=triggers,
        )


RECORD_FORMAT = "<8sLL144B"


def unpack_record(buffer: bytes, offset: int = 0) -> Tuple[Record, int]:
    """Unpacks a single AnimData record from the `buffer` at `offset`."""
    (cof_name, frames_per_direction, animation_speed, *frame_data) = struct.unpack_from(
        RECORD_FORMAT, buffer, offset=offset
    )

    assert all(
        ch == 0 for ch in cof_name[cof_name.index(b"\0") :]
    ), f"{cof_name} has non-null character after null terminator"

    triggers = []
    for frame_index, frame_code in enumerate(frame_data):
        if frame_code:
            assert frame_index < frames_per_direction, (
                f"Trigger frame {frame_index}={frame_code} "
                f"appears after end of animation (length={frames_per_direction})"
            )
            triggers.append(ActionTrigger(frame=frame_index, code=frame_code))

    return (
        Record(
            cof_name=str(cof_name.split(b"\0", maxsplit=1)[0], encoding="ascii"),
            frames_per_direction=frames_per_direction,
            animation_speed=animation_speed,
            triggers=tuple(triggers),
        ),
        struct.calcsize(RECORD_FORMAT),
    )


DWORD_MAX = 0xFFFFFFFF


def pack_record(record: Record) -> bytes:
    """Packs a single AnimData record."""
    cof_name = bytes(record.cof_name, encoding="ascii")
    if len(cof_name) != 7:
        raise ValueError(
            f"COF name must be exactly 7 bytes."
            f" ({cof_name!r} is {len(cof_name)} bytes long)"
        )
    if b"\0" in cof_name:
        raise ValueError(
            f"COF name must not contain a null character. (found in {cof_name!r})"
        )

    frames_per_direction = record.frames_per_direction
    if not 0 <= frames_per_direction <= DWORD_MAX:
        raise ValueError(
            f"frames_per_direction must be between 0 and {DWORD_MAX}."
            f"(current value is {frames_per_direction!r})"
        )

    animation_speed = record.animation_speed
    if not 0 <= animation_speed <= DWORD_MAX:
        raise ValueError(
            f"animation_speed must be between 0 and {DWORD_MAX}."
            f"(current value is {animation_speed!r})"
        )

    frame_data = [0] * 144
    for trigger in record.triggers:
        if not 1 <= trigger.code <= 3:
            raise ValueError(f"Invalid trigger code {trigger.code!r} in {record!r}")
        if trigger.frame >= frames_per_direction:
            raise ValueError(
                f"Trigger frame must be less than or equal to frames_per_direction "
                f" (got trigger frame={trigger.frame!r}, "
                f"frames_per_direction={frames_per_direction} "
                f"in {record!r})"
            )
        try:
            if frame_data[trigger.frame] != 0:
                raise ValueError(
                    f"Cannot assign a trigger {trigger!r} "
                    f"to a frame already in use, in {record!r}"
                )
            frame_data[trigger.frame] = trigger.code
        except IndexError:
            raise ValueError(
                f"Trigger frame must be between 0 and {len(frame_data)} "
                f"(got {trigger.frame!r} in {record!r}"
            ) from None

    return struct.pack(
        RECORD_FORMAT, cof_name, frames_per_direction, animation_speed, *frame_data
    )


def sort_records_by_cof_name(records: List[Record]) -> None:
    """Sorts a list of Records in place by COF name."""
    records.sort(key=lambda record: record.cof_name)


def check_duplicate_cof_names(records: Iterable[Record]) -> None:
    """Checks if the list of AnimData records contains duplicate COF names."""
    cof_names_seen = set()
    for record in records:
        if record.cof_name in cof_names_seen:
            logger.warning(f"Duplicate entry found: {record.cof_name}")
        else:
            cof_names_seen.add(record.cof_name)


RECORD_COUNT_FORMAT = "<L"


def loads(data: bytes) -> List[Record]:
    """Loads the contents of AnimData.D2 from binary `data`.

    Args:
        file:
            Contents of AnimData.D2 in binary format.

    Returns:
        List of Record objects, ordered by their original order in the `data`.
    """
    blocks = []
    offset = 0
    for block_index in range(256):
        (record_count,) = struct.unpack_from(RECORD_COUNT_FORMAT, data, offset=offset)
        offset += struct.calcsize(RECORD_COUNT_FORMAT)

        records = []
        for _ in range(record_count):
            record, record_size = unpack_record(data, offset=offset)
            hash_value = hash_cof_name(record.cof_name)
            assert block_index == hash_value, (
                f"Incorrect hash (COF name={record.cof_name}): "
                f"expected {block_index} but got {hash_value}"
            )
            records.append(record)
            offset += record_size

        blocks.append(records)

    assert offset == len(data), (
        f"Data size mismatch: "
        f"Blocks use {offset} bytes, but binary size is {len(data)} bytes"
    )

    return list(itertools.chain.from_iterable(blocks))


def load(file: BinaryIO) -> List[Record]:
    """Loads the contents of AnimData.D2 from the a binary file.

    Args:
        file:
            Readable file object opened in binary mode.

    Returns:
        List of Record objects.
    """
    return loads(file.read())


def dumps(records: Iterable[Record]) -> bytearray:
    """Packs AnimData records into AnimData.D2 hash table format."""
    hash_table = [[] for _ in range(256)]
    for record in records:
        hash_value = hash_cof_name(record.cof_name)
        hash_table[hash_value].append(record)

    packed_data = bytearray()
    for block in hash_table:
        packed_data += struct.pack(RECORD_COUNT_FORMAT, len(block))
        for record in block:
            packed_data += pack_record(record)

    return packed_data


def dump(records: Iterable[Record], file: BinaryIO) -> None:
    """Packs AnimData records into binary format and writes them to a file."""
    file.write(dumps(records))


def load_txt(file: Iterable[str]) -> List[Record]:
    """Loads AnimData records from a tabbed text file."""
    records = []
    for row_num, row in enumerate(csv.DictReader(file, dialect="excel-tab")):
        try:
            cof_name = row["CofName"]
            frames_per_direction = int(row["FramesPerDirection"])
            animation_speed = int(row["AnimationSpeed"])
            triggers = []
            for frame in range(144):
                code = int(row[f"FrameData{frame:03}"])
                if code:
                    triggers.append(ActionTrigger(frame=frame, code=code))
            records.append(
                Record(
                    cof_name=cof_name,
                    frames_per_direction=frames_per_direction,
                    animation_speed=animation_speed,
                    triggers=tuple(triggers),
                )
            )
        except (KeyError, TypeError, ValueError, csv.Error) as err:
            raise LoadTxtError("Failed to parse TXT file", row=row_num) from err
    return records


def dump_txt(records: Iterable[Record], file: TextIO) -> None:
    """Saves AnimData records to a tabbed text file."""
    writer = csv.writer(file, dialect="excel-tab")
    writer.writerow(
        [
            "CofName",
            "FramesPerDirection",
            "AnimationSpeed",
            *(f"FrameData{frame:03}" for frame in range(144)),
        ]
    )

    for record in records:
        row = [
            record.cof_name,
            record.frames_per_direction,
            record.animation_speed,
        ]
        frame_data = [0] * 144
        for trigger in record.triggers:
            frame_data[trigger.frame] = trigger.code
        row += frame_data
        writer.writerow(row)


def main(argv: List[str]) -> None:
    """Entrypoint for the CLI script."""
    logging.basicConfig(format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command")

    parser_compile = subparsers.add_parser(
        "compile", help="Compiles JSON to AnimData.D2"
    )
    parser_compile.add_argument("source", help="JSON or tabbed text file to compile")
    parser_compile.add_argument("animdata_d2", help="AnimData.D2 file to save to")
    parser_compile.add_argument(
        "--sort",
        action="store_true",
        help="Sort the records alphabetically before saving",
    )

    format_group = parser_compile.add_mutually_exclusive_group(required=True)
    format_group.add_argument("--json", action="store_true", help="Compile JSON")
    format_group.add_argument(
        "--txt", action="store_true", help="Compile tabbed text (TXT)"
    )

    parser_decompile = subparsers.add_parser(
        "decompile", help="Deompiles AnimData.D2 to JSON"
    )
    parser_decompile.add_argument("animdata_d2", help="AnimData.D2 file to decompile")
    parser_decompile.add_argument("target", help="JSON or tabbed text file to save to")
    parser_decompile.add_argument(
        "--sort",
        action="store_true",
        help="Sort the records alphabetically before saving",
    )

    format_group = parser_decompile.add_mutually_exclusive_group(required=True)
    format_group.add_argument("--json", action="store_true", help="Decompile to JSON")
    format_group.add_argument(
        "--txt", action="store_true", help="Decompile to tabbed text (TXT)"
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
    elif args.command == "compile":
        if args.txt:
            with open(args.source, newline="") as source_file:
                records = load_txt(source_file)
        elif args.json:
            with open(args.source) as source_file:
                json_data = json.load(source_file)
            records = list(map(Record.from_dict, json_data))
        else:
            raise ValueError("No file format specified")

        check_duplicate_cof_names(records)
        if args.sort:
            sort_records_by_cof_name(records)

        with open(args.animdata_d2, mode="wb") as animdata_d2_file:
            dump(records, animdata_d2_file)
    elif args.command == "decompile":
        with open(args.animdata_d2, mode="rb") as animdata_d2_file:
            records = load(animdata_d2_file)

        check_duplicate_cof_names(records)
        if args.sort:
            sort_records_by_cof_name(records)

        if args.txt:
            with open(args.target, mode="w", newline="") as target_file:
                dump_txt(records, target_file)
        elif args.json:
            json_data = [record.make_dict() for record in records]
            with open(args.target, mode="w") as target_file:
                json.dump(json_data, target_file, indent=2)
        else:
            raise ValueError("No file format specified")
    else:
        raise ValueError(f"Unexpected command: {args.command!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
