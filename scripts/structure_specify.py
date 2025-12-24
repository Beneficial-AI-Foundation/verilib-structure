#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-frontmatter"
# ]
# ///
"""
Check specification status of functions and manage specification certs.

For dalek-lite type:
    1. Runs scip-atoms specify to check which functions have specs (requires/ensures)
    2. Filters to only functions listed in the structure (from config.json)
    3. Compares with existing specification certs in .verilib/certs/specify/
    4. Shows functions with specs that don't have certs yet
    5. Lets user select functions to validate and create certs for

For blueprint type:
    1. Reads blueprint.json and checks type-status for specs (stated, mathlib)
    2. Compares with existing specification certs in .verilib/certs/specify/
    3. Shows functions with specs that don't have certs yet
    4. Lets user select functions to validate and create certs for

Usage:
    uv run scripts/structure_specify.py [project_root]
    uv run scripts/structure_specify.py /path/to/project
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import frontmatter


# SCIP configuration
SCIP_ATOMS_REPO = "https://github.com/Beneficial-AI-Foundation/scip-atoms"
SCIP_PREFIX = "curve25519-dalek"


def check_scip_atoms_or_exit() -> None:
    """Check if scip-atoms is installed, exit with instructions if not."""
    installed = shutil.which("scip-atoms") is not None
    if not installed:
        print("Error: scip-atoms is not installed.")
        print(f"Please visit {SCIP_ATOMS_REPO} for installation instructions.")
        print("\nQuick install:")
        print("  git clone", SCIP_ATOMS_REPO)
        print("  cd scip-atoms")
        print("  cargo install --path .")
        raise SystemExit(1)


def encode_scip_name(scip_name: str) -> str:
    """
    Encode a scip-name for use as a filename.

    Uses URL percent-encoding to replace special characters like '/', ':', '#', etc.

    Args:
        scip_name: The SCIP identifier (e.g., "scip:curve25519-dalek/4.1.3/module#func()")

    Returns:
        Encoded string safe for use as a filename.

    Example:
        >>> encode_scip_name("scip:crate/1.0/mod#func()")
        "scip%3Acrate%2F1.0%2Fmod%23func%28%29"
    """
    return quote(scip_name, safe='')


def decode_scip_name(encoded: str) -> str:
    """
    Decode a filename back to a scip-name.

    Args:
        encoded: URL percent-encoded filename.

    Returns:
        Original scip-name.
    """
    return unquote(encoded)


def run_scip_specify(project_root: Path, specs_path: Path, atoms_path: Path) -> dict:
    """
    Run scip-atoms specify and return the results.

    Args:
        project_root: Root directory of the project to analyze.
        specs_path: Path where specs.json will be written.
        atoms_path: Path to atoms.json for scip-name lookup.

    Returns:
        Dictionary of specification data from scip-atoms.

    Raises:
        SystemExit: If scip-atoms is not installed or fails to run.
    """
    check_scip_atoms_or_exit()

    # Ensure output directory exists
    specs_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Running scip-atoms specify on {project_root}...")
    result = subprocess.run(
        ["scip-atoms", "specify", SCIP_PREFIX,
         "--json-output", str(specs_path),
         "--with-scip-names", str(atoms_path)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: scip-atoms specify failed.")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(1)

    print(f"Specs saved to {specs_path}")

    with open(specs_path, encoding='utf-8') as f:
        return json.load(f)


def get_functions_with_specs(specs_data: dict) -> dict[str, dict]:
    """
    Filter specs data to only functions that have requires or ensures.

    Args:
        specs_data: Dictionary from scip-atoms specify output.
                    Expected format: dict mapping scip-name to function info.

    Returns:
        Dictionary mapping scip-name to spec data for functions with specs.
    """
    result = {}

    for scip_name, func_info in specs_data.items():
        has_requires = func_info.get('has_requires', False)
        has_ensures = func_info.get('has_ensures', False)

        if has_requires or has_ensures:
            result[scip_name] = func_info

    return result


# Type-status values that indicate a function has a spec
BLUEPRINT_SPEC_STATUSES = {'stated', 'mathlib'}


def get_blueprint_functions_with_specs(blueprint_path: Path) -> dict[str, dict]:
    """
    Get functions with specs from blueprint.json based on type-status.

    Functions with type-status 'stated' or 'mathlib' are considered to have specs.

    Args:
        blueprint_path: Path to blueprint.json file.

    Returns:
        Dictionary mapping veri-name to blueprint node data for functions with specs.
    """
    if not blueprint_path.exists():
        print(f"Warning: {blueprint_path} not found")
        return {}

    with open(blueprint_path, encoding='utf-8') as f:
        blueprint_data = json.load(f)

    result = {}
    for node_id, node_info in blueprint_data.items():
        type_status = node_info.get('type-status', '')
        if type_status in BLUEPRINT_SPEC_STATUSES:
            veri_name = f"veri:{node_id}"
            result[veri_name] = node_info

    return result


def get_existing_certs(certs_dir: Path) -> set[str]:
    """
    Get the set of func_ids that already have certs.

    Args:
        certs_dir: Path to the .verilib/certs/specify/ directory.

    Returns:
        Set of func_ids that have existing cert files.
    """
    if not certs_dir.exists():
        return set()

    existing = set()
    for cert_file in certs_dir.glob("*.json"):
        # Remove .json extension and decode
        encoded_name = cert_file.stem
        func_id = decode_scip_name(encoded_name)
        existing.add(func_id)

    return existing


def get_structure_names(
    structure_type: str,
    structure_form: str,
    structure_root: Path,
    structure_json_path: Path
) -> set[str]:
    """
    Get the set of identifier names from the structure.

    Args:
        structure_type: Either "dalek-lite" or "blueprint".
        structure_form: Either "json" or "files".
        structure_root: Path to the structure root directory (for files form).
        structure_json_path: Path to structure_files.json (for json form).

    Returns:
        Set of identifier names defined in the structure (scip-name or veri-name).
    """
    # Determine which field to look for based on structure type
    name_field = "veri-name" if structure_type == "blueprint" else "scip-name"

    names = set()

    if structure_form == "json":
        if not structure_json_path.exists():
            print(f"Warning: {structure_json_path} not found")
            return names

        with open(structure_json_path, encoding='utf-8') as f:
            structure = json.load(f)

        for entry in structure.values():
            name = entry.get(name_field)
            if name:
                names.add(name)

    elif structure_form == "files":
        if not structure_root.exists():
            print(f"Warning: {structure_root} not found")
            return names

        for md_file in structure_root.rglob("*.md"):
            post = frontmatter.load(md_file)
            name = post.get(name_field)
            if name:
                names.add(name)

    return names


def create_cert(certs_dir: Path, func_id: str) -> Path:
    """
    Create a specification cert file for a function.

    Args:
        certs_dir: Path to the .verilib/certs/specify/ directory.
        func_id: The function identifier (file:start_line).

    Returns:
        Path to the created cert file.
    """
    certs_dir.mkdir(parents=True, exist_ok=True)

    encoded_name = encode_scip_name(func_id)
    cert_path = certs_dir / f"{encoded_name}.json"

    cert_data = {
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    with open(cert_path, 'w', encoding='utf-8') as f:
        json.dump(cert_data, f, indent=2)

    return cert_path


def display_menu(functions: list[tuple[str, dict]], structure_type: str) -> list[int]:
    """
    Display a multiple choice menu and get user selections.

    Args:
        functions: List of (func_id, func_info) tuples to display.
        structure_type: Either "dalek-lite" or "blueprint".

    Returns:
        List of selected indices (0-based).
    """
    print("\n" + "=" * 60)
    print("Functions with specs but no certification:")
    print("=" * 60 + "\n")

    for i, (name, func_info) in enumerate(functions, 1):
        if structure_type == "blueprint":
            # Blueprint: show veri-name, kind, and type-status
            kind = func_info.get('kind', '?')
            type_status = func_info.get('type-status', '?')
            # Extract node_id from veri-name (remove "veri:" prefix)
            node_id = name[5:] if name.startswith("veri:") else name
            print(f"  [{i}] {node_id}")
            print(f"      Kind: {kind}, Status: {type_status}")
        else:
            # Dalek-lite: show function name, file, line, and spec types
            has_requires = func_info.get('has_requires', False)
            has_ensures = func_info.get('has_ensures', False)

            spec_types = []
            if has_requires:
                spec_types.append("requires")
            if has_ensures:
                spec_types.append("ensures")

            spec_str = ", ".join(spec_types)
            func_name = func_info.get('name', '?')
            file_path = func_info.get('file', '?')
            start_line = func_info.get('start_line', '?')
            print(f"  [{i}] {func_name} ({file_path}:{start_line})")
            print(f"      Specs: {spec_str}")
        print()

    print("=" * 60)
    print("\nEnter selection:")
    print("  - Individual numbers: 1, 3, 5")
    print("  - Ranges: 1-5")
    print("  - 'all' to select all")
    print("  - 'none' or empty to skip")
    print()

    try:
        user_input = input("Your selection: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return []

    if not user_input or user_input == 'none':
        return []

    if user_input == 'all':
        return list(range(len(functions)))

    # Parse selection
    selected = set()
    parts = user_input.replace(',', ' ').split()

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if '-' in part:
            # Range
            try:
                start, end = part.split('-', 1)
                start_idx = int(start) - 1
                end_idx = int(end) - 1
                for i in range(start_idx, end_idx + 1):
                    if 0 <= i < len(functions):
                        selected.add(i)
            except ValueError:
                print(f"Warning: Invalid range '{part}', skipping")
        else:
            # Single number
            try:
                idx = int(part) - 1
                if 0 <= idx < len(functions):
                    selected.add(idx)
                else:
                    print(f"Warning: {part} out of range, skipping")
            except ValueError:
                print(f"Warning: Invalid number '{part}', skipping")

    return sorted(selected)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Check specification status and manage specification certs"
    )
    parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    return parser.parse_args()


def main() -> None:
    """
    Check specification status and manage specification certs.

    For dalek-lite:
        1. Read config to get structure form and root
        2. Run scip-atoms specify to get spec data
        3. Filter to functions with specs (has_requires or has_ensures)
        4. Filter to only functions in the structure
        5. Compare with existing certs
        6. Show user uncertified functions and let them choose which to certify
        7. Create cert files for selected functions

    For blueprint:
        1. Read config and blueprint.json
        2. Get functions with specs (type-status: stated or mathlib)
        3. Compare with existing certs
        4. Show user uncertified functions and let them choose which to certify
        5. Create cert files for selected functions
    """
    args = parse_args()

    # Resolve project root to absolute path
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"
    certs_dir = verilib_path / "certs" / "specify"
    config_path = verilib_path / "config.json"
    structure_json_path = verilib_path / "structure_files.json"

    # Read config file
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run structure_create.py first.", file=sys.stderr)
        raise SystemExit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    structure_type = config.get("structure-type")
    structure_form = config.get("structure-form")
    if structure_form not in ("json", "files"):
        print(f"Error: Unknown form '{structure_form}'", file=sys.stderr)
        raise SystemExit(1)

    structure_root_relative = config.get("structure-root", ".verilib")
    structure_root = project_root / structure_root_relative

    if structure_type == "blueprint":
        # Blueprint type: get specs from blueprint.json based on type-status
        blueprint_path = verilib_path / "blueprint.json"
        functions_with_specs = get_blueprint_functions_with_specs(blueprint_path)
        print(f"\nFound {len(functions_with_specs)} functions with specs in blueprint")

        # Get veri-names from structure
        structure_names = get_structure_names(
            structure_type, structure_form, structure_root, structure_json_path
        )
        print(f"Found {len(structure_names)} functions in structure")

        # Filter to only functions in structure
        functions_in_structure = {
            veri_name: spec_info
            for veri_name, spec_info in functions_with_specs.items()
            if veri_name in structure_names
        }
        print(f"Found {len(functions_in_structure)} functions with specs in structure")

    else:
        # Dalek-lite type: run scip-atoms specify
        specs_path = verilib_path / "specs.json"
        atoms_path = verilib_path / "atoms.json"
        specs_data = run_scip_specify(project_root, specs_path, atoms_path)

        # Get functions with specs
        functions_with_specs = get_functions_with_specs(specs_data)
        print(f"\nFound {len(functions_with_specs)} functions with specs in codebase")

        # Get scip-names from structure
        structure_names = get_structure_names(
            structure_type, structure_form, structure_root, structure_json_path
        )
        print(f"Found {len(structure_names)} functions in structure")

        # Filter to only functions in structure
        functions_in_structure = {
            scip_name: spec_info
            for scip_name, spec_info in functions_with_specs.items()
            if scip_name in structure_names
        }
        print(f"Found {len(functions_in_structure)} functions with specs in structure")

    # Get existing certs
    existing_certs = get_existing_certs(certs_dir)
    print(f"Found {len(existing_certs)} existing certs")

    # Find uncertified functions
    uncertified = {
        name: spec_info
        for name, spec_info in functions_in_structure.items()
        if name not in existing_certs
    }

    if not uncertified:
        print("\nAll functions with specs in structure are already validated!")
        return

    print(f"\n{len(uncertified)} functions with specs need certification")

    # Sort by name for consistent display
    uncertified_list = sorted(uncertified.items(), key=lambda x: x[0])

    # Display menu and get selection
    selected_indices = display_menu(uncertified_list, structure_type)

    if not selected_indices:
        print("\nNo functions selected.")
        return

    # Create certs for selected functions
    print(f"\nCreating certs for {len(selected_indices)} functions...")

    for idx in selected_indices:
        name, _ = uncertified_list[idx]
        cert_path = create_cert(certs_dir, name)
        print(f"  Created: {cert_path.name}")

    print(f"\nDone. Created {len(selected_indices)} cert files in {certs_dir}")


if __name__ == "__main__":
    main()
