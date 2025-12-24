#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "intervaltree",
#   "python-frontmatter"
# ]
# ///
"""
Update verilib structure files by syncing with SCIP atoms.

This script:
1. Runs scip-atoms to generate source code intelligence data
2. Filters atoms by crate prefix (curve25519-dalek)
3. Builds an interval tree index for line-based lookups
4. Syncs structure files or JSON with SCIP atom data

Usage:
    uv run scripts/structure_atomize.py [project_root]
"""

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import requests
import frontmatter
from intervaltree import IntervalTree

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


def generate_scip_atoms(project_root: Path, atoms_path: Path) -> dict[str, dict]:
    """
    Run scip-atoms on the project and save results to atoms.json.

    Executes the scip-atoms CLI tool to analyze source code and generate
    SCIP (Source Code Intelligence Protocol) data.

    Args:
        project_root: Root directory of the project to analyze.
        atoms_path: Path where atoms.json will be written.

    Returns:
        Dictionary mapping scip-name to atom data. Each atom contains:
        - code-path: Relative path to source file
        - code-text: Dict with lines-start, lines-end, and source text
        - code-module: Module path extracted from scip-name
        - dependencies: List of scip-names this function depends on

    Raises:
        SystemExit: If scip-atoms is not installed or fails to run.
    """
    check_scip_atoms_or_exit()

    # Ensure output directory exists
    atoms_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Running scip-atoms on {project_root}...")
    result = subprocess.run(
        ["scip-atoms", "atoms", str(project_root), "-o", str(atoms_path), "-r"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: scip-atoms failed.")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(1)

    # Clean up generated intermediate files
    for cleanup_file in ["data/index.scip", "data/index.scip.json"]:
        cleanup_path = project_root / cleanup_file
        if cleanup_path.exists():
            cleanup_path.unlink()

    # Remove data/ folder if empty
    data_dir = project_root / "data"
    if data_dir.exists() and data_dir.is_dir() and not any(data_dir.iterdir()):
        data_dir.rmdir()

    print(f"Results saved to {atoms_path}")

    # Read the generated JSON file (already a dict keyed by scip-name)
    with open(atoms_path, encoding='utf-8') as f:
        return json.load(f)


def generate_scip_index(scip_atoms: dict[str, dict]) -> dict[str, IntervalTree]:
    """
    Build an interval tree index for fast line-based lookups.

    Creates a dictionary of IntervalTree structures, one per source file,
    allowing efficient lookup of which function contains a given line number.

    Args:
        scip_atoms: Dictionary mapping scip-name to atom data.

    Returns:
        Dictionary mapping code-path to IntervalTree. Each tree stores
        intervals [lines-start, lines-end+1) with the scip-name as data.

    Example:
        >>> index = generate_scip_index(atoms)
        >>> tree = index["src/lib.rs"]
        >>> matches = tree[42]  # Find all functions containing line 42
    """
    trees: dict[str, IntervalTree] = defaultdict(IntervalTree)

    for scip_name, atom_data in scip_atoms.items():
        code_path = atom_data.get('code-path')
        code_text = atom_data.get('code-text', {})

        if not code_path:
            continue

        lines_start = code_text.get('lines-start')
        lines_end = code_text.get('lines-end')

        if lines_start is None or lines_end is None:
            continue

        # IntervalTree uses half-open intervals [start, end)
        # Add 1 to lines_end to make it inclusive
        interval_end = lines_end + 1

        trees[code_path].addi(lines_start, interval_end, scip_name)

    return dict(trees)


def filter_scip_atoms(scip_atoms: dict[str, dict], prefix: str) -> dict[str, dict]:
    """
    Filter SCIP atoms to only those where scip-name starts with prefix.

    Args:
        scip_atoms: Dictionary mapping scip-name to atom data
        prefix: Crate name to filter by (e.g., "curve25519-dalek")

    Returns:
        Dictionary mapping scip-name to the atom object, filtered by prefix
    """
    # scip-name format: "scip:<crate>/<version>/<path>"
    uri_prefix = f"scip:{prefix}/"
    return {
        scip_name: atom_data
        for scip_name, atom_data in scip_atoms.items()
        if scip_name.startswith(uri_prefix)
    }


def _update_entry_from_atoms(
    entry: dict,
    scip_index: dict[str, IntervalTree],
    scip_atoms: dict[str, dict],
    context: str = ""
) -> tuple[dict | None, str | None]:
    """
    Update a structure entry with SCIP atom data.

    Common logic for syncing structure entries (either from .md files or JSON dict).

    Args:
        entry: Dictionary with 'code-path', 'code-line', and optionally 'scip-name'
        scip_index: Dictionary mapping code-path to IntervalTree
        scip_atoms: Dictionary mapping scip-name to atom data
        context: Optional context string for warning messages (e.g., file path)

    Returns:
        Tuple of (updated_entry, error_message).
        If successful, updated_entry contains the updated values and error_message is None.
        If failed, updated_entry is None and error_message describes the issue.
    """
    code_path = entry.get('code-path')
    line_start = entry.get('code-line')
    existing_scip_name = entry.get('scip-name')

    updated = dict(entry)

    # If scip-name exists and is in scip_atoms, verify against it
    if existing_scip_name and existing_scip_name in scip_atoms:
        atom = scip_atoms[existing_scip_name]
        atom_code_path = atom.get('code-path')
        atom_code_text = atom.get('code-text', {})
        atom_line_start = atom_code_text.get('lines-start')

        # Verify code-path matches
        if code_path != atom_code_path:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: code-path mismatch{ctx}: "
                  f"'{code_path}' will be overwritten with '{atom_code_path}'")

        # Verify code-line matches
        if line_start != atom_line_start:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: code-line mismatch{ctx}: "
                  f"{line_start} will be overwritten with {atom_line_start}")

        # Update with values from scip_atoms
        updated['code-path'] = atom_code_path
        updated['code-line'] = atom_line_start

    else:
        # scip-name missing or not found - look up by code-path and code-line
        if existing_scip_name:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: scip-name '{existing_scip_name}' not found in scip_atoms{ctx}, "
                  f"looking up by code-path/code-line")
        # No scip-name, look up by code-path and code-line
        if not code_path or line_start is None:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: Missing code-path or code-line{ctx}; scip-name will not be generated")
            return updated, None

        if code_path not in scip_index:
            return None, f"code-path '{code_path}' not found in scip_index"

        tree = scip_index[code_path]

        # Find intervals that contain the start line
        matching_intervals = tree[line_start]

        # Filter to only intervals that start exactly at line_start
        exact_matches = [iv for iv in matching_intervals if iv.begin == line_start]

        if not exact_matches:
            return None, f"No interval starting at line {line_start} in {code_path}"

        if len(exact_matches) > 1:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: Multiple intervals starting at line {line_start} in {code_path}{ctx}")

        # Use the first match
        interval = exact_matches[0]
        updated['scip-name'] = interval.data

    return updated, None


def sync_structure_files_with_atoms(
    scip_index: dict[str, IntervalTree],
    scip_atoms: dict[str, dict],
    structure_root: Path
) -> None:
    """
    Sync structure .md files with SCIP atoms index.

    For each .md file in structure_root:
    - If it has a scip-name, look it up in scip_atoms and verify code-path/code-line match
    - If it doesn't have a scip-name, look up by code-path and code-line in scip_index

    Args:
        scip_index: Dictionary mapping code-path to IntervalTree
        scip_atoms: Dictionary mapping scip-name to atom data
        structure_root: Path to the .verilib directory
    """
    updated_count = 0
    not_found_count = 0

    for md_file in structure_root.rglob("*.md"):
        post = frontmatter.load(md_file)

        entry = dict(post.metadata)

        updated, error = _update_entry_from_atoms(entry, scip_index, scip_atoms, str(md_file))

        if error:
            print(f"WARNING: {error} for {md_file}")
            not_found_count += 1
            continue

        # Update the frontmatter
        post['code-path'] = updated['code-path']
        post['code-line'] = updated['code-line']
        if 'scip-name' in updated:
            post['scip-name'] = updated['scip-name']

        # Write updated content
        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(frontmatter.dumps(post))

        updated_count += 1

    print(f"Structure files updated: {updated_count}")
    print(f"Not found/skipped: {not_found_count}")


def sync_structure_json_with_atoms(
    structure: dict[str, dict],
    scip_index: dict[str, IntervalTree],
    scip_atoms: dict[str, dict],
) -> dict[str, dict]:
    """
    Sync structure dictionary with SCIP atoms index.

    For each entry in structure:
    - If it has a scip-name, look it up in scip_atoms and verify code-path/code-line match
    - If it doesn't have a scip-name, look up by code-path and code-line in scip_index

    Args:
        structure: Dictionary mapping file_path to metadata dict with keys:
                   code-path, code-line, scip-name
        scip_index: Dictionary mapping code-path to IntervalTree
        scip_atoms: Dictionary mapping scip-name to atom data

    Returns:
        Updated structure dictionary with synced values.
    """
    updated_count = 0
    not_found_count = 0
    result = {}

    for file_path, entry in structure.items():
        updated, error = _update_entry_from_atoms(entry, scip_index, scip_atoms, file_path)

        if error:
            print(f"WARNING: {error} for {file_path}")
            not_found_count += 1
            # Keep original entry on error
            result[file_path] = entry
            continue

        result[file_path] = updated
        updated_count += 1

    print(f"Structure entries updated: {updated_count}")
    print(f"Not found/skipped: {not_found_count}")

    return result


def _generate_metadata_from_atom(
    scip_name: str,
    scip_atoms: dict[str, dict],
) -> tuple[dict | None, str | None]:
    """
    Generate metadata dict from SCIP atom data.

    Common helper for populate_structure_files_metadata and populate_structure_json_metadata.

    Args:
        scip_name: The SCIP identifier.
        scip_atoms: Dictionary mapping scip-name to atom data.

    Returns:
        Tuple of (metadata_dict, error_message).
        If successful, metadata_dict contains the metadata and error_message is None.
        If failed, metadata_dict is None and error_message describes the issue.

        metadata_dict contains:
            - code-path: Relative path to source file
            - code-lines: {start, end} line numbers
            - code-module: Module path from atom data
            - dependencies: List of scip-names this function depends on
            - specified: Boolean flag (always False)
            - visible: Boolean flag (always True)
    """
    if not scip_name or scip_name not in scip_atoms:
        return None, "Missing or invalid scip-name"

    atom = scip_atoms[scip_name]
    code_path = atom.get('code-path')
    code_text = atom.get('code-text', {})
    lines_start = code_text.get('lines-start')
    lines_end = code_text.get('lines-end')
    dependencies = atom.get('dependencies', [])

    if not code_path or lines_start is None or lines_end is None:
        return None, "Missing code-path or line info"

    # Get code-module from atom data
    code_module = atom.get('code-module', '')

    meta_data = {
        "code-path": code_path,
        "code-lines": {
            "start": lines_start,
            "end": lines_end
        },
        "code-module": code_module,
        "dependencies": dependencies,
        "specified": False,
        "visible": True
    }

    return meta_data, None


def populate_structure_files_metadata(
    scip_atoms: dict[str, dict],
    structure_root: Path,
    project_root: Path
) -> None:
    """
    Generate metadata files for each structure .md file.

    For each XXX.md file in structure_root, creates two companion files:

    XXX.meta.verilib (JSON):
        - code-path: Relative path to source file
        - code-lines: {start, end} line numbers
        - code-module: Module path (reversed segments from scip-name)
        - dependencies: List of scip-names this function depends on
        - scip-name: The SCIP identifier
        - visible: Boolean flag (always True)

    XXX.atom.verilib:
        - Raw source code extracted from the original file

    Args:
        scip_atoms: Dictionary mapping scip-name to atom data.
        structure_root: Path to the .verilib directory containing .md files.
        project_root: Path to project root for resolving source file paths.
    """
    created_count = 0
    skipped_count = 0

    for md_file in structure_root.rglob("*.md"):
        post = frontmatter.load(md_file)
        scip_name = post.get('scip-name')

        meta_data, error = _generate_metadata_from_atom(scip_name, scip_atoms)

        if error:
            print(f"WARNING: {error} for {md_file}")
            skipped_count += 1
            continue

        # Add scip-name to meta_data for files version
        meta_data["scip-name"] = scip_name

        # Write XXX.meta.verilib
        meta_file = md_file.with_suffix('.meta.verilib')
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta_data, f, indent=2)

        # Create XXX.atom.verilib by extracting source code
        code_path = meta_data["code-path"]
        lines_start = meta_data["code-lines"]["start"]
        lines_end = meta_data["code-lines"]["end"]

        source_file = project_root / code_path
        if not source_file.exists():
            print(f"WARNING: Source file not found: {source_file}")
            skipped_count += 1
            continue

        with open(source_file, encoding='utf-8') as f:
            all_lines = f.readlines()

        # Extract lines (1-indexed, inclusive)
        extracted_lines = all_lines[lines_start - 1:lines_end]
        atom_content = ''.join(extracted_lines)

        atom_file = md_file.with_suffix('.atom.verilib')
        with open(atom_file, 'w', encoding='utf-8') as f:
            f.write(atom_content)

        created_count += 1

    print(f"Metadata files created: {created_count}")
    print(f"Skipped: {skipped_count}")


def populate_structure_json_metadata(
    structure: dict[str, dict],
    scip_atoms: dict[str, dict],
) -> dict[str, dict]:
    """
    Generate metadata dictionary from structure JSON.

    For each entry in structure with a valid scip-name, creates a metadata dict.
    Unlike populate_structure_files_metadata, this does not extract atom text.

    Args:
        structure: Dictionary mapping file_path to metadata dict with scip-name.
        scip_atoms: Dictionary mapping scip-name to atom data.

    Returns:
        Dictionary mapping scip-name to metadata dict with:
            - code-path: Relative path to source file
            - code-lines: {start, end} line numbers
            - code-module: Module path (reversed segments from scip-name)
            - dependencies: List of scip-names this function depends on
            - specified: Boolean flag (always False)
            - visible: Boolean flag (always True)
    """
    result = {}
    created_count = 0
    skipped_count = 0

    for file_path, entry in structure.items():
        scip_name = entry.get('scip-name')

        meta_data, error = _generate_metadata_from_atom(scip_name, scip_atoms)

        if error:
            print(f"WARNING: {error} for {file_path}")
            skipped_count += 1
            continue

        result[scip_name] = meta_data
        created_count += 1

    print(f"Metadata entries created: {created_count}")
    print(f"Skipped: {skipped_count}")

    return result


def structure_to_tree(structure_root: Path) -> list[dict]:
    """
    Convert structure files to a JSON tree format.

    Walks the structure root folder recursively and builds a nested tree structure
    where:
    - Folder hierarchy determines the children relationships
    - Identifiers use paths derived from .md file locations
    - Content comes from .atom.verilib files
    - Dependencies are mapped from scip-names to paths

    Args:
        structure_root: Path to the .verilib directory containing structure files.

    Returns:
        List of root tree nodes. Each node is a dict with:
        - identifier: Path for files, folder path for folders
        - content: Source code text (empty for folders)
        - children: List of child nodes
        - file_type: "file" or "folder"
        - dependencies: List of dependency paths (files only)
        - specified: Boolean flag (files only)
    """
    # First pass: build scip-name to path mapping
    scip_to_path: dict[str, str] = {}
    for md_file in structure_root.rglob("*.md"):
        meta_file = md_file.with_suffix(".meta.verilib")
        if not meta_file.exists():
            continue

        with open(meta_file, encoding="utf-8") as f:
            meta_data = json.load(f)

        scip_name = meta_data.get("scip-name", "")
        if not scip_name:
            continue

        # Construct path from .md file location relative to structure_root
        relative_path = md_file.relative_to(structure_root)
        # Remove .md extension and prepend /
        path = "/" + str(relative_path.with_suffix(""))
        scip_to_path[scip_name] = path

    def build_tree_recursive(dir_path: Path, parent_identifier: str = "") -> list[dict]:
        """Recursively build tree nodes from directory structure."""
        nodes = []

        # Get all items in directory, sorted for consistent ordering
        items = sorted(dir_path.iterdir())

        # Separate directories and files
        subdirs = [item for item in items if item.is_dir()]
        md_files = [item for item in items if item.is_file() and item.suffix == ".md"]

        # Process subdirectories as folder nodes
        for subdir in subdirs:
            folder_name = subdir.name
            folder_identifier = f"{parent_identifier}/{folder_name}" if parent_identifier else folder_name

            children = build_tree_recursive(subdir, folder_identifier)

            # Only add folder if it has children
            if children:
                folder_node = {
                    "identifier": folder_identifier,
                    "content": "",
                    "children": children,
                    "file_type": "folder",
                }
                nodes.append(folder_node)

        # Process .md files as file nodes
        for md_file in md_files:
            meta_file = md_file.with_suffix(".meta.verilib")
            atom_file = md_file.with_suffix(".atom.verilib")

            # Skip if meta file doesn't exist
            if not meta_file.exists():
                continue

            # Read metadata
            with open(meta_file, encoding="utf-8") as f:
                meta_data = json.load(f)

            scip_name = meta_data.get("scip-name", "")
            if not scip_name:
                continue

            # Get identifier from path mapping
            identifier = scip_to_path.get(scip_name, "")
            if not identifier:
                continue

            # Get content from atom file
            content = ""
            if atom_file.exists():
                content = atom_file.read_text(encoding="utf-8")

            # Get dependencies and map to paths
            scip_dependencies = meta_data.get("dependencies", [])
            dependencies = [scip_to_path.get(dep, dep) for dep in scip_dependencies]

            # Get specified flag
            specified = meta_data.get("specified", False)

            file_node = {
                "identifier": identifier,
                "content": content,
                "children": [],
                "file_type": "file",
                "dependencies": dependencies,
                "specified": specified,
            }
            nodes.append(file_node)

        return nodes

    # Build tree starting from structure_root
    return build_tree_recursive(structure_root)


def deploy(url, repo, api_key, tree, debug_path=None):
    """Send POST request to deploy endpoint.

    Args:
        url: Base URL of the deployment server
        repo: Repository ID
        tree: Tree structure to deploy
        api_key: API key for authorization
        debug: If True, write json_body to file for debugging
        debug_path: Path to the debug log file
    """
    # Create json_body from tree
    json_body = {"tree": tree}

    deploy_url = f"{url}/v2/repo/deploy/{repo}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"ApiKey {api_key}",
    }

    # Write json_body to file with pretty printing (only if debug mode)
    if debug_path:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write("\n=== deploy json_body ===\n")
            json.dump(json_body, f, indent=4)
            f.write("\n")
        print(f"json_body written to {debug_path}")

    # Send POST request with JSON body
    response = requests.post(deploy_url, headers=headers, json=json_body)

    # Print response details
    print(f"Status Code: {response.status_code}")
    print(f"Response Headers: {dict(response.headers)}")
    print(f"Response Body: {response.text}")

    return response


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Update verilib structure by syncing with SCIP atoms"
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
    Update verilib structure by syncing with SCIP atoms.

    Steps:
        1. Parse command line arguments
        2. Generate SCIP atoms from source code
        3. Filter to only curve25519-dalek atoms
        4. Build interval tree index for line lookups
        5. Sync structure (JSON or files) with SCIP data
    """
    args = parse_args()

    # Resolve project root to absolute path
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"

    # Read config file
    config_path = verilib_path / "config.json"
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run structure_create.py first.", file=sys.stderr)
        raise SystemExit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    structure_type = config.get("structure-type")
    if structure_type != "dalek-lite":
        print(f"Error: Unknown type '{structure_type}'", file=sys.stderr)
        raise SystemExit(1)

    structure_form = config.get("structure-form")
    if structure_form not in ("json", "files"):
        print(f"Error: Unknown form '{structure_form}'", file=sys.stderr)
        raise SystemExit(1)

    # Get structure root from config (relative to project root)
    structure_root_relative = config.get("structure-root", ".verilib")
    structure_root = project_root / structure_root_relative

    # Compute paths
    atoms_path = verilib_path / "atoms.json"
    structure_json_path = verilib_path / "structure_files.json"
    structure_meta_path = verilib_path / "structure_meta.json"

    # Generate and sync SCIP atoms
    scip_atoms = generate_scip_atoms(project_root, atoms_path)
    scip_atoms = filter_scip_atoms(scip_atoms, SCIP_PREFIX)
    scip_index = generate_scip_index(scip_atoms)

    if structure_form == "json":
        # Load structure_files.json, sync, and save
        if not structure_json_path.exists():
            print(f"Error: {structure_json_path} not found", file=sys.stderr)
            raise SystemExit(1)

        print(f"Loading structure from {structure_json_path}...")
        with open(structure_json_path, encoding='utf-8') as f:
            structure = json.load(f)

        print("Syncing structure with SCIP atoms...")
        structure = sync_structure_json_with_atoms(structure, scip_index, scip_atoms)

        print(f"Saving updated structure to {structure_json_path}...")
        with open(structure_json_path, 'w', encoding='utf-8') as f:
            json.dump(structure, f, indent=2)

        print("Populating structure metadata...")
        metadata = populate_structure_json_metadata(structure, scip_atoms)

        print(f"Saving metadata to {structure_meta_path}...")
        with open(structure_meta_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        print("Done.")

    elif structure_form == "files":
        print(f"Syncing structure files in {structure_root} with SCIP atoms...")
        sync_structure_files_with_atoms(scip_index, scip_atoms, structure_root)

        print("Populating structure metadata files...")
        populate_structure_files_metadata(scip_atoms, structure_root, project_root)
        print("Done.")


if __name__ == "__main__":
    main()
