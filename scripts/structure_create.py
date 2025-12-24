#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beartype",
# ]
# ///
"""
Initialize verilib structure files from functions_to_track.csv.

Analyzes source code using analyze_verus_specs_proofs to identify tracked functions,
then generates .md structure files with YAML frontmatter containing code-path,
code-line, and scip-name fields.

Usage:
    uv run scripts/structure_create.py --type dalek-lite --form files --root .verilib
    uv run scripts/structure_create.py --type dalek-lite --form json
"""

import argparse
import io
import json
import sys
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from analyze_verus_specs_proofs import analyze_functions

def tweak_disambiguate(tracked: dict) -> dict:
    """
    Disambiguate tracked items that have the same qualified_name.

    When multiple functions share the same qualified_name, appends a numeric
    suffix to make them unique: XXX becomes XXX_0, XXX_1, ..., XXX_N.

    Args:
        tracked: Dictionary mapping key to tuple of function metadata.
                 Index 5 of each tuple is the qualified_name.

    Returns:
        New dictionary with disambiguated qualified_names in the tuples.
    """
    qualified_names = [value[5] for value in tracked.values()]
    name_counts = Counter(qualified_names)

    # Find duplicates
    duplicates = {name for name, count in name_counts.items() if count > 1}

    if not duplicates:
        return tracked

    # Track current index for each duplicate name
    name_indices: dict[str, int] = {name: 0 for name in duplicates}

    # Create new tracked dict with disambiguated names
    new_tracked = {}
    for key, value in tracked.items():
        qualified_name = value[5]
        if qualified_name in duplicates:
            # Create new name with suffix
            new_name = f"{qualified_name}_{name_indices[qualified_name]}"
            name_indices[qualified_name] += 1
            # Create new tuple with updated qualified_name
            new_value = value[:5] + (new_name,) + value[6:]
            new_tracked[key] = new_value
        else:
            new_tracked[key] = value

    return new_tracked


def parse_github_link(github_link: str) -> tuple[str, int]:
    """
    Extract code path and line number from a GitHub link.

    Args:
        github_link: GitHub URL of form ".../blob/main/<path>#L<line>"

    Returns:
        Tuple of (code_path, line_number). Returns ("", 0) if parsing fails.

    Example:
        >>> parse_github_link("https://github.com/org/repo/blob/main/src/lib.rs#L42")
        ("src/lib.rs", 42)
    """
    if not github_link or "/blob/main/" not in github_link:
        return "", 0

    path_part = github_link.split("/blob/main/")[1]
    if "#L" in path_part:
        code_path, line_str = path_part.rsplit("#L", 1)
        return code_path, int(line_str)
    return path_part, 0


def tracked_to_structure(tracked: dict) -> dict[str, dict]:
    """
    Convert tracked functions to a structure dictionary.

    Creates a dictionary mapping file paths to metadata dictionaries for each
    function in the tracked dictionary.

    Args:
        tracked: Dictionary mapping key to tuple of function metadata.
                 Index 4 is the github_link, index 5 is the qualified_name.

    Returns:
        Dictionary mapping file_path (str) to dict with keys:
            - code-path: Relative path to source file
            - code-line: Line number where function starts
            - scip-name: null (populated later by structure_atomize.py)
    """
    result = {}

    for value in tracked.values():
        github_link, qualified_name = value[4], value[5]
        code_path, line_start = parse_github_link(github_link)

        if not code_path:
            continue

        # Convert qualified_name to filename (replace :: with .)
        func_name = qualified_name.replace('::', '.')
        file_path = f"{code_path}/{func_name}.md"

        result[file_path] = {
            "code-line": line_start,
            "code-path": code_path,
            "scip-name": None,
        }

    return result


def _format_yaml_value(value) -> str:
    """
    Format a Python value as a YAML scalar.

    Args:
        value: A scalar value (str, int, float, bool, None, or list of scalars).

    Returns:
        YAML-formatted string representation.

    Raises:
        ValueError: If value is a nested dict.
    """
    if isinstance(value, dict):
        raise ValueError(f"Nested dicts are not supported in metadata: {value}")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        # Format as YAML inline list
        formatted_items = [_format_yaml_value(item) for item in value]
        return "[" + ", ".join(formatted_items) + "]"
    # String - quote if it contains special characters or could be misinterpreted
    s = str(value)
    if (s in ("null", "true", "false", "~", "") or
        s.startswith(("{", "[", "'", '"', "|", ">", "*", "&", "!")) or
        ":" in s or "#" in s or "\n" in s):
        # Use double quotes and escape special chars
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return s


def generate_structure_files(structure: dict[str, dict], structure_root: Path) -> None:
    """
    Generate structure .md files from a structure dictionary.

    Creates a markdown file with YAML frontmatter for each entry in the structure
    dictionary. Files are organized by code path under the verilib directory.

    Args:
        structure: Dictionary mapping file_path to flat metadata dict.
                   Values must not be nested dicts.
        structure_root: Directory where structure files will be created.

    Raises:
        ValueError: If any metadata value is a nested dict.
    """
    created_count = 0

    for relative_path_str, metadata in structure.items():
        file_path = structure_root / relative_path_str
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists():
            print(f"WARNING: File already exists, overwriting: {file_path}")

        # Build YAML frontmatter from metadata
        yaml_lines = ["---"]
        for key, value in metadata.items():
            formatted_value = _format_yaml_value(value)
            yaml_lines.append(f"{key}: {formatted_value}")
        yaml_lines.append("---")
        yaml_lines.append("")

        content = "\n".join(yaml_lines)
        file_path.write_text(content, encoding='utf-8')
        created_count += 1

    print(f"Created {created_count} structure files in {structure_root}")


def generate_structure_json(structure: dict[str, dict], output_path: Path) -> None:
    """
    Write structure dictionary to a JSON file.

    Args:
        structure: Dictionary mapping file_path (str) to flat metadata dict.
        output_path: Path to write the JSON file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(structure, indent=2), encoding='utf-8')
    print(f"Wrote structure to {output_path}")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate verilib structure files from functions_to_track.csv"
    )
    parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    parser.add_argument(
        "--type",
        choices=["dalek-lite"],
        required=True,
        help="Type of the source to analyze"
    )
    parser.add_argument(
        "--form",
        choices=["json", "files"],
        required=True,
        help="Structure form: 'json' writes to structure_files.json, 'files' creates .md file hierarchy"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Root directory for structure files (default: <project_root>/.verilib)"
    )
    return parser.parse_args()


def main() -> None:
    """
    Initialize verilib structure files from functions_to_track.csv.

    Steps:
        1. Parse command line arguments
        2. Analyze source code to identify tracked functions
        3. Disambiguate duplicate function names
        4. Generate structure output (JSON or files)
    """
    args = parse_args()

    # Resolve project root to absolute path
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"

    # Set default root if not provided (relative to project root)
    if args.root is None:
        structure_root_relative = ".verilib"
    else:
        structure_root_relative = str(args.root)

    # Compute paths
    tracked_path = project_root / "functions_to_track.csv"
    structure_json_path = verilib_path / "structure_files.json"

    if not tracked_path.exists():
        print(f"Error: {tracked_path} not found", file=sys.stderr)
        sys.exit(1)

    print("Analyzing source code to derive list of functions to track...")

    if args.type == "dalek-lite":
        with redirect_stdout(io.StringIO()):
            tracked = analyze_functions(tracked_path, project_root)
        tracked = tweak_disambiguate(tracked)
        structure = tracked_to_structure(tracked)
    else:
        print(f"Error: Unknown type '{args.type}'", file=sys.stderr)
        sys.exit(1)

    # Write config file with structure type, form, and root
    config_path = verilib_path / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "structure-type": args.type,
        "structure-form": args.form,
        "structure-root": structure_root_relative,
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote config to {config_path}")

    print("\nGenerating structure output...")
    if args.form == "json":
        generate_structure_json(structure, structure_json_path)
    elif args.form == "files":
        generate_structure_files(structure, project_root / structure_root_relative)


if __name__ == "__main__":
    main()
