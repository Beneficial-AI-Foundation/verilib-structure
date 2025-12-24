#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-frontmatter"
# ]
# ///
"""
Run verification and manage verification certs.

For dalek-lite type:
    1. Runs scip-atoms verify to check which functions pass verification
    2. Filters to only functions listed in the structure (from config.json)
    3. Compares with existing verification certs in .verilib/certs/verify/
    4. Creates new certs for newly verified functions
    5. Deletes certs for functions that now fail verification
    6. Shows a summary of all changes

For blueprint type:
    1. Reads blueprint.json and checks term-status for verification
       (only 'fully-proved' is considered verified)
    2. Compares with existing verification certs in .verilib/certs/verify/
    3. Creates new certs for newly verified functions
    4. Deletes certs for functions that now fail verification
    5. Shows a summary of all changes

Usage:
    uv run scripts/structure_verify.py [project_root]
    uv run scripts/structure_verify.py /path/to/project
    uv run scripts/structure_verify.py --verify-only-module edwards
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


def run_scip_verify(
    project_root: Path,
    verification_path: Path,
    atoms_path: Path,
    verify_only_module: str | None = None
) -> dict:
    """
    Run scip-atoms verify and return the results.

    Args:
        project_root: Root directory of the project to analyze.
        verification_path: Path where verification.json will be written.
        atoms_path: Path to atoms.json for scip-name lookup.
        verify_only_module: Optional module name to verify only that module.

    Returns:
        Dictionary of verification data from scip-atoms.

    Raises:
        SystemExit: If scip-atoms is not installed or fails to run.
    """
    check_scip_atoms_or_exit()

    # Ensure output directory exists
    verification_path.parent.mkdir(parents=True, exist_ok=True)

    # Build command
    cmd = [
        "scip-atoms", "verify", SCIP_PREFIX,
        "--json-output", str(verification_path),
        "--with-scip-names", str(atoms_path)
    ]
    if verify_only_module:
        cmd.extend(["--verify-only-module", verify_only_module])

    if verify_only_module:
        print(f"Running scip-atoms verify on {project_root} (module: {verify_only_module})...")
    else:
        print(f"Running scip-atoms verify on {project_root}...")

    result = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: scip-atoms verify failed.")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(1)

    print(f"Verification results saved to {verification_path}")

    with open(verification_path, encoding='utf-8') as f:
        return json.load(f)


def get_verification_results(verification_data: dict) -> tuple[set[str], set[str]]:
    """
    Extract verified and failed function scip-names from verification data.

    Args:
        verification_data: Dictionary from scip-atoms verify output.

    Returns:
        Tuple of (verified_scip_names, failed_scip_names).
    """
    verification = verification_data.get('verification', {})

    verified = set()
    for func in verification.get('verified_functions', []):
        scip_name = func.get('scip-name')
        if scip_name:
            verified.add(scip_name)

    failed = set()
    for func in verification.get('failed_functions', []):
        scip_name = func.get('scip-name')
        if scip_name:
            failed.add(scip_name)

    return verified, failed


# Term-status values that indicate a function is verified
BLUEPRINT_VERIFIED_STATUSES = {'fully-proved'}


def get_blueprint_verification_results(blueprint_path: Path) -> tuple[set[str], set[str]]:
    """
    Extract verified and failed veri-names from blueprint.json based on term-status.

    Only 'fully-proved' term-status is considered verified.
    All other term-statuses are considered failed.

    Args:
        blueprint_path: Path to blueprint.json file.

    Returns:
        Tuple of (verified_veri_names, failed_veri_names).
    """
    if not blueprint_path.exists():
        print(f"Warning: {blueprint_path} not found")
        return set(), set()

    with open(blueprint_path, encoding='utf-8') as f:
        blueprint_data = json.load(f)

    verified = set()
    failed = set()

    for node_id, node_info in blueprint_data.items():
        veri_name = f"veri:{node_id}"
        term_status = node_info.get('term-status', '')

        if term_status in BLUEPRINT_VERIFIED_STATUSES:
            verified.add(veri_name)
        else:
            failed.add(veri_name)

    return verified, failed


def get_existing_certs(certs_dir: Path) -> set[str]:
    """
    Get the set of scip-names that already have verification certs.

    Args:
        certs_dir: Path to the .verilib/certs/verify/ directory.

    Returns:
        Set of scip-names that have existing cert files.
    """
    if not certs_dir.exists():
        return set()

    existing = set()
    for cert_file in certs_dir.glob("*.json"):
        # Remove .json extension and decode
        encoded_name = cert_file.stem
        scip_name = decode_scip_name(encoded_name)
        existing.add(scip_name)

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


def create_cert(certs_dir: Path, scip_name: str) -> Path:
    """
    Create a verification cert file for a function.

    Args:
        certs_dir: Path to the .verilib/certs/verify/ directory.
        scip_name: The scip-name of the function.

    Returns:
        Path to the created cert file.
    """
    certs_dir.mkdir(parents=True, exist_ok=True)

    encoded_name = encode_scip_name(scip_name)
    cert_path = certs_dir / f"{encoded_name}.json"

    cert_data = {
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    with open(cert_path, 'w', encoding='utf-8') as f:
        json.dump(cert_data, f, indent=2)

    return cert_path


def delete_cert(certs_dir: Path, scip_name: str) -> Path | None:
    """
    Delete a verification cert file for a function.

    Args:
        certs_dir: Path to the .verilib/certs/verify/ directory.
        scip_name: The scip-name of the function.

    Returns:
        Path to the deleted cert file, or None if it didn't exist.
    """
    encoded_name = encode_scip_name(scip_name)
    cert_path = certs_dir / f"{encoded_name}.json"

    if cert_path.exists():
        cert_path.unlink()
        return cert_path

    return None


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run verification and manage verification certs"
    )
    parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    parser.add_argument(
        "--verify-only-module",
        type=str,
        default=None,
        help="Only verify functions in this module (e.g., 'edwards')"
    )
    return parser.parse_args()


def main() -> None:
    """
    Run verification and manage verification certs.

    For dalek-lite:
        1. Read config to get structure form and root
        2. Run scip-atoms verify to get verification data
        3. Filter to only functions in the structure
        4. Compare with existing certs
        5. Create new certs for verified functions without certs
        6. Delete certs for failed functions with existing certs
        7. Show summary of changes

    For blueprint:
        1. Read config and blueprint.json
        2. Get verification results based on term-status (fully-proved = verified)
        3. Filter to only functions in the structure
        4. Compare with existing certs
        5. Create/delete certs as needed
        6. Show summary of changes
    """
    args = parse_args()

    # Resolve project root to absolute path
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"
    certs_dir = verilib_path / "certs" / "verify"
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
        # Blueprint type: get verification results from blueprint.json
        if args.verify_only_module:
            print("Warning: --verify-only-module is ignored for blueprint type")

        blueprint_path = verilib_path / "blueprint.json"
        verified_funcs, failed_funcs = get_blueprint_verification_results(blueprint_path)

    elif structure_type == "dalek-lite":
        # Dalek-lite type: run scip-atoms verify
        verification_path = verilib_path / "verification.json"
        atoms_path = verilib_path / "atoms.json"
        verification_data = run_scip_verify(
            project_root, verification_path, atoms_path,
            verify_only_module=args.verify_only_module
        )
        verified_funcs, failed_funcs = get_verification_results(verification_data)

    else:
        print(f"Error: Unknown structure type '{structure_type}'", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nVerification summary:")
    print(f"  Verified: {len(verified_funcs)}")
    print(f"  Failed: {len(failed_funcs)}")

    # Get names from structure
    structure_names = get_structure_names(
        structure_type, structure_form, structure_root, structure_json_path
    )
    print(f"  Functions in structure: {len(structure_names)}")

    # Filter to only functions in structure
    verified_in_structure = verified_funcs & structure_names
    failed_in_structure = failed_funcs & structure_names
    print(f"  Verified in structure: {len(verified_in_structure)}")
    print(f"  Failed in structure: {len(failed_in_structure)}")

    # Get existing certs
    existing_certs = get_existing_certs(certs_dir)
    print(f"  Existing certs: {len(existing_certs)}")

    # Determine changes needed (limited to functions in structure only)
    # New certs: verified functions in structure without existing certs
    to_create = verified_in_structure - existing_certs
    # Delete certs: failed functions in structure with existing certs
    to_delete = failed_in_structure & existing_certs

    # Apply changes
    created = []
    deleted = []

    for name in sorted(to_create):
        cert_path = create_cert(certs_dir, name)
        created.append((name, cert_path))

    for name in sorted(to_delete):
        cert_path = delete_cert(certs_dir, name)
        if cert_path:
            deleted.append((name, cert_path))

    # Helper to extract display name
    def get_display_name(name: str) -> str:
        if name.startswith("veri:"):
            return name[5:]  # Remove "veri:" prefix
        else:
            return name.split('#')[-1].rstrip('()')  # scip-name format

    # Show summary
    print("\n" + "=" * 60)
    print("VERIFICATION CERT CHANGES")
    print("=" * 60)

    if created:
        print(f"\n✓ Created {len(created)} new certs:")
        for name, cert_path in created:
            display_name = get_display_name(name)
            print(f"  + {display_name}")
            print(f"    {name}")
    else:
        print("\n✓ No new certs created")

    if deleted:
        print(f"\n✗ Deleted {len(deleted)} certs (verification failed):")
        for name, cert_path in deleted:
            display_name = get_display_name(name)
            print(f"  - {display_name}")
            print(f"    {name}")
    else:
        print("\n✓ No certs deleted")

    # Final summary
    print("\n" + "=" * 60)
    final_certs = len(existing_certs) + len(created) - len(deleted)
    print(f"Total certs: {len(existing_certs)} → {final_certs}")
    print(f"  Created: +{len(created)}")
    print(f"  Deleted: -{len(deleted)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
