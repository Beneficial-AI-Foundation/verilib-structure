#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "leanblueprint",
#   "lxml",
#   "requests",
#   "intervaltree",
#   "python-frontmatter"
# ]
# ///
"""
Unified CLI for verilib structure management.

This script provides four subcommands for managing verification structure files:

    create   - Initialize structure files from source analysis
    atomize  - Enrich structure files with metadata
    specify  - Check specification status and manage spec certs
    verify   - Run verification and manage verification certs

For dalek-lite type:
    - Analyzes Verus/Rust code via analyze_verus_specs_proofs.py
    - Uses scip-atoms for code intelligence and verification

For blueprint type:
    - Runs leanblueprint web and parses dependency graph
    - Uses blueprint.json for specs and verification status

Usage:
    uv run scripts/structure.py create --type dalek-lite
    uv run scripts/structure.py create --type blueprint
    uv run scripts/structure.py atomize [project_root]
    uv run scripts/structure.py specify [project_root]
    uv run scripts/structure.py verify [project_root] [--verify-only-module <module>]
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import frontmatter
import requests
from intervaltree import IntervalTree
from lxml import html


# =============================================================================
# CONSTANTS
# =============================================================================

PROBE_VERUS_REPO = "https://github.com/Beneficial-AI-Foundation/probe-verus"
PROBE_PREFIX = "curve25519-dalek"

# Type-status values that indicate a function has a spec
BLUEPRINT_SPEC_STATUSES = {'stated', 'mathlib'}

# Term-status values that indicate a function is verified
BLUEPRINT_VERIFIED_STATUSES = {'fully-proved'}


# =============================================================================
# COMMON UTILITIES
# =============================================================================

def check_probe_verus_or_exit() -> None:
    """Check if probe-verus is installed, exit with instructions if not."""
    installed = shutil.which("probe-verus") is not None
    if not installed:
        print("Error: probe-verus is not installed.")
        print(f"Please visit {PROBE_VERUS_REPO} for installation instructions.")
        print("\nQuick install:")
        print("  git clone", PROBE_VERUS_REPO)
        print("  cd probe-verus")
        print("  cargo install --path .")
        raise SystemExit(1)


def encode_name(name: str) -> str:
    """
    Encode an identifier for use as a filename.

    Uses URL percent-encoding to replace special characters like '/', ':', '#', etc.

    Args:
        name: The identifier (code-name or veri-name)

    Returns:
        Encoded string safe for use as a filename.
    """
    return quote(name, safe='')


def decode_name(encoded: str) -> str:
    """
    Decode a filename back to an identifier.

    Args:
        encoded: URL percent-encoded filename.

    Returns:
        Original identifier (code-name or veri-name).
    """
    return unquote(encoded)


def get_existing_certs(certs_dir: Path) -> set[str]:
    """
    Get the set of identifiers that already have certs.

    Args:
        certs_dir: Path to the certs directory (e.g., .verilib/certs/verify/).

    Returns:
        Set of identifiers that have existing cert files.
    """
    if not certs_dir.exists():
        return set()

    existing = set()
    for cert_file in certs_dir.glob("*.json"):
        # Remove .json extension and decode
        encoded_name = cert_file.stem
        name = decode_name(encoded_name)
        existing.add(name)

    return existing


def create_cert(certs_dir: Path, name: str) -> Path:
    """
    Create a cert file for a function.

    Args:
        certs_dir: Path to the certs directory.
        name: The identifier (code-name or veri-name).

    Returns:
        Path to the created cert file.
    """
    certs_dir.mkdir(parents=True, exist_ok=True)

    encoded_name = encode_name(name)
    cert_path = certs_dir / f"{encoded_name}.json"

    cert_data = {
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    with open(cert_path, 'w', encoding='utf-8') as f:
        json.dump(cert_data, f, indent=2)

    return cert_path


def get_structure_names(
    structure_type: str,
    structure_root: Path,
) -> set[str]:
    """
    Get the set of identifier names from the structure files.

    Args:
        structure_type: Either "dalek-lite" or "blueprint".
        structure_root: Path to the structure root directory.

    Returns:
        Set of identifier names defined in the structure (code-name or veri-name).
    """
    # Determine which field to look for based on structure type
    name_field = "veri-name" if structure_type == "blueprint" else "code-name"

    names = set()

    if not structure_root.exists():
        print(f"Warning: {structure_root} not found")
        return names

    for md_file in structure_root.rglob("*.md"):
        post = frontmatter.load(md_file)
        name = post.get(name_field)
        if name:
            names.add(name)

    return names


def load_config(project_root: Path) -> dict:
    """
    Load config.json and return config with computed paths.

    Args:
        project_root: Root directory of the project.

    Returns:
        Dictionary with config values and computed paths:
            - structure-type: "dalek-lite" or "blueprint"
            - structure-root: Relative path string
            - verilib_path: Absolute Path to .verilib
            - structure_root: Absolute Path to structure root
            - structure_json_path: Absolute Path to stubs.json
            - certs_specify_dir: Absolute Path to certs/specify/
            - certs_verify_dir: Absolute Path to certs/verify/

    Raises:
        SystemExit: If config.json not found or has invalid values.
    """
    verilib_path = project_root / ".verilib"
    config_path = verilib_path / "config.json"

    if not config_path.exists():
        print(f"Error: {config_path} not found. Run 'structure.py create' first.", file=sys.stderr)
        raise SystemExit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    structure_root_relative = config.get("structure-root", ".verilib")

    # Add computed paths
    config["verilib_path"] = verilib_path
    config["structure_root"] = project_root / structure_root_relative
    config["structure_json_path"] = verilib_path / "stubs.json"
    config["certs_specify_dir"] = verilib_path / "certs" / "specify"
    config["certs_verify_dir"] = verilib_path / "certs" / "verify"

    return config


# =============================================================================
# CREATE SUBCOMMAND FUNCTIONS
# =============================================================================

# -----------------------------------------------------------------------------
# Blueprint dependency graph extraction
# -----------------------------------------------------------------------------

def parse_attributes(attr_string: str) -> dict[str, Any]:
    """Parse attribute string like '[color=red, shape=box]' into a dictionary."""
    attrs = {}
    attr_string = attr_string.strip('[]')
    for attr in attr_string.split(','):
        attr = attr.strip()
        if '=' in attr:
            key, value = attr.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"\'')
            attrs[key] = value
    return attrs


def get_node_info(dom) -> dict[str, str]:
    """
    Extract all IDs and content of div elements with class "dep-modal-container".

    Args:
        dom: The parsed DOM object (lxml.html.HtmlElement)

    Returns:
        dict: Mapping of node IDs to their HTML content
    """
    modal_divs = dom.xpath('//div[@class="dep-modal-container"]')
    ids = {}
    for div in modal_divs:
        div_id = div.get('id')
        if len(div) != 1:
            raise ValueError(f"Div has {len(div)} children, expected 1")
        div_content = html.tostring(div[0], pretty_print=True, encoding='unicode')
        if div_id.endswith('_modal'):
            div_id = div_id.replace('_modal', '')
            ids[div_id] = div_content
        else:
            raise ValueError(f"Div ID does not end with _modal: {div_id}")
    return ids


def get_dep_graph_string(dom) -> str | None:
    """
    Extract the renderDot content string from the DOM object.

    Args:
        dom: The parsed DOM object (lxml.html.HtmlElement)

    Returns:
        str: The renderDot content string, or None if not found
    """
    script_elements = dom.xpath('//script')
    for script in script_elements:
        script_text = script.text_content() if script.text else ""
        if ".renderDot(`strict digraph" in script_text:
            start_pattern = r"\.renderDot\(`strict digraph"
            match = re.search(start_pattern, script_text)
            if match:
                start_pos = match.start()
                extracted_content = script_text[start_pos:]
                dot_pattern = r"\.renderDot\(`([^`]*)`\)"
                dot_match = re.search(dot_pattern, extracted_content)
                if dot_match:
                    return dot_match.group(1)
    return None


def parse_node_element(line: str) -> dict[str, Any] | None:
    """Parse a node element line and return node information."""
    bracket_pos = line.find('[')
    if bracket_pos == -1:
        return None
    node_id_part = line[:bracket_pos].strip()
    node_id = node_id_part.strip('"\'')
    attr_start = line.find('[')
    attr_end = line.rfind(']')
    if attr_start == -1 or attr_end == -1:
        return {'id': node_id, 'attributes': {}}
    attr_string = line[attr_start:attr_end + 1]
    attributes = parse_attributes(attr_string)
    return {'id': node_id, 'attributes': attributes}


def parse_edge_element(line: str) -> dict[str, Any] | None:
    """Parse an edge element line and return edge information."""
    arrow_pos = line.find('->')
    if arrow_pos == -1:
        return None
    source = line[:arrow_pos].strip().strip('"\'')
    remaining = line[arrow_pos + 2:].strip()
    attr_start = remaining.find('[')
    if attr_start != -1:
        target = remaining[:attr_start].strip().strip('"\'')
        attr_end = remaining.rfind(']')
        if attr_end != -1:
            attr_string = remaining[attr_start:attr_end + 1]
            attributes = parse_attributes(attr_string)
        else:
            attributes = {}
    else:
        target = remaining.strip('"\'')
        attributes = {}
    attributes['source'] = source
    attributes['target'] = target
    return {'id': f"{source}->{target}", 'attributes': attributes}


def get_dep_graph(dom, node_info: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """
    Parse the renderDot content from DOM and create a dictionary of nodes.

    Args:
        dom: The parsed DOM object (lxml.html.HtmlElement)
        node_info: Optional mapping of node IDs to content

    Returns:
        Dictionary of nodes with ID as key and attributes as value
    """
    renderdot_string = get_dep_graph_string(dom)
    if not renderdot_string:
        print("Could not extract renderDot content from DOM")
        return {}

    if not renderdot_string.startswith('strict digraph "" {'):
        print("Content doesn't start with expected pattern")
        return {}

    start_pos = len('strict digraph "" {')
    end_pos = renderdot_string.rfind('}')
    if end_pos == -1:
        print("Could not find closing brace")
        return {}

    content = renderdot_string[start_pos:end_pos]
    elements = content.split(';')
    edges = {}
    nodes = {}

    for element in elements:
        element = element.strip()
        if not element:
            continue

        if element.startswith('graph [') or element.startswith('node [') or element.startswith('edge ['):
            continue

        if '[' in element and ']' in element and '->' not in element:
            node_attr = parse_node_element(element)
            if node_attr:
                attributes = node_attr['attributes']
                attributes['kind'] = ""
                attributes['content'] = ""
                attributes['type-status'] = ""
                attributes['term-status'] = ""
                attributes['type-dependencies'] = []
                attributes['term-dependencies'] = []

                if 'shape' in attributes:
                    if attributes['shape'] == 'ellipse':
                        attributes['kind'] = 'theorem'
                    elif attributes['shape'] == 'box':
                        attributes['kind'] = 'definition'
                    else:
                        raise ValueError(f"Unknown shape: {attributes['shape']}")
                    attributes.pop('shape')
                else:
                    raise ValueError(f"Node missing shape attribute")

                if 'color' in attributes:
                    color_map = {
                        'green': 'stated',
                        'blue': 'can-state',
                        '#FFAA33': 'not-ready',
                        'darkgreen': 'mathlib',
                    }
                    attributes['type-status'] = color_map.get(attributes['color'], 'unrecognized')
                    attributes.pop('color')
                else:
                    attributes['type-status'] = 'unknown'

                if 'fillcolor' in attributes:
                    fillcolor_map = {
                        '#9CEC8B': 'proved',
                        '#B0ECA3': 'defined',
                        '#A3D6FF': 'can-prove',
                        '#1CAC78': 'fully-proved',
                    }
                    attributes['term-status'] = fillcolor_map.get(attributes['fillcolor'], 'unrecognized')
                    attributes.pop('fillcolor')
                else:
                    attributes['term-status'] = 'unknown'

                if 'label' in attributes:
                    if attributes['label'] != node_attr['id']:
                        raise ValueError(f"Node label mismatch: {attributes['label']} != {node_attr['id']}")
                    attributes.pop('label')
                else:
                    raise ValueError(f"No label found in node attributes")

                if 'style' in attributes:
                    if attributes['style'] != 'filled':
                        raise ValueError(f"Unknown style: {attributes['style']}")
                    attributes.pop('style')

                nodes[node_attr['id']] = attributes

        elif '->' in element:
            edge_info = parse_edge_element(element)
            if edge_info:
                edges[edge_info['id']] = edge_info['attributes']

    if node_info:
        for node_id, content in node_info.items():
            if node_id not in nodes:
                raise ValueError(f"Node ID '{node_id}' from node_info not found in parsed nodes")
            nodes[node_id]['content'] = content

    if edges:
        for _, attributes in edges.items():
            source = attributes['source']
            target = attributes['target']
            if source not in nodes or target not in nodes:
                raise ValueError(f"Source or target node not found: {source} or {target}")
            if 'style' in attributes and attributes['style'] == 'dashed':
                nodes[source]['type-dependencies'].append(target)
            else:
                nodes[source]['term-dependencies'].append(target)

    return nodes


def check_leanblueprint_installed() -> bool:
    """Check if leanblueprint CLI tool is installed and accessible."""
    return shutil.which("leanblueprint") is not None


def run_leanblueprint_web(project_root: Path) -> None:
    """
    Run 'leanblueprint web' to generate the blueprint/web folder.

    Args:
        project_root: The project root directory to run the command from.

    Raises:
        subprocess.CalledProcessError: If the command fails.
    """
    print("Running 'leanblueprint web' to generate blueprint...")
    result = subprocess.run(
        ["leanblueprint", "web"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error running leanblueprint web:\n{result.stderr}", file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, "leanblueprint web")
    print("Successfully generated blueprint/web")


def generate_blueprint_json(project_root: Path, output_path: Path) -> dict[str, dict[str, Any]]:
    """
    Parse blueprint/web/dep_graph_document.html and generate blueprint.json.

    Args:
        project_root: The project root directory.
        output_path: Path to write the blueprint.json file.

    Returns:
        The parsed dependency graph dictionary.
    """
    html_path = project_root / "blueprint" / "web" / "dep_graph_document.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {html_path}...")
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    dom = html.fromstring(html_content)
    node_info = get_node_info(dom)
    print(f"Found {len(node_info)} dep-modal-container elements")

    nodes = get_dep_graph(dom, node_info)
    print(f"Parsed {len(nodes)} nodes from dependency graph")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(nodes, f, indent=2, ensure_ascii=False)
    print(f"Wrote blueprint data to {output_path}")

    return nodes


def blueprint_to_structure(blueprint_data: dict[str, dict[str, Any]]) -> dict[str, dict]:
    """
    Convert blueprint data to a structure dictionary.

    Args:
        blueprint_data: Dictionary mapping blueprint IDs to node attributes.

    Returns:
        Dictionary mapping file_path (str) to dict with keys:
            - veri-name: "veri:<blueprint-id>"
            - dependencies: List of veri-names
            - content: Content from blueprint data
    """
    result = {}
    for blueprint_id, attributes in blueprint_data.items():
        file_path = f"{blueprint_id}.md"
        type_deps = attributes.get("type-dependencies", [])
        term_deps = attributes.get("term-dependencies", [])
        all_deps = [f"veri:{dep}" for dep in type_deps + term_deps]
        result[file_path] = {
            "veri-name": f"veri:{blueprint_id}",
            "dependencies": all_deps,
            "content": attributes.get("content", ""),
        }
    return result


# -----------------------------------------------------------------------------
# Dalek-lite functions
# -----------------------------------------------------------------------------

def run_analyze_verus_specs_proofs(
    project_root: Path, seed_path: Path, output_path: Path
) -> None:
    """
    Run analyze_verus_specs_proofs.py CLI to generate tracked functions CSV.

    Args:
        project_root: The project root directory.
        seed_path: Path to the input functions_to_track.csv file.
        output_path: Path to write the output CSV file.

    Raises:
        subprocess.CalledProcessError: If the command fails.
        FileNotFoundError: If the script is not found.
    """
    script_path = project_root / "scripts" / "analyze_verus_specs_proofs.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    print(f"Running analyze_verus_specs_proofs.py...")
    result = subprocess.run(
        [
            "uv", "run", str(script_path),
            "--seed", str(seed_path.relative_to(project_root)),
            "--output", str(output_path.relative_to(project_root)),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error running analyze_verus_specs_proofs.py:\n{result.stderr}", file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, "analyze_verus_specs_proofs.py")
    print(f"Generated tracked functions CSV at {output_path}")


def read_tracked_csv(csv_path: Path) -> dict[str, tuple]:
    """
    Read tracked functions CSV and return a dictionary.

    The CSV has columns: function, module, link, has_spec, has_proof

    Args:
        csv_path: Path to the tracked functions CSV file.

    Returns:
        Dictionary mapping unique key to tuple of function metadata.
    """
    results = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            function = row['function']
            module = row['module']
            link = row['link']
            has_spec_val = row['has_spec']
            has_proof_val = row['has_proof']

            has_spec = has_spec_val in ('yes', 'ext')
            is_external_body = has_spec_val == 'ext'
            has_proof = has_proof_val == 'yes'

            line_number = 0
            if link and '#L' in link:
                try:
                    line_number = int(link.rsplit('#L', 1)[1])
                except ValueError:
                    pass

            result_key = f"{function}::{module}"
            results[result_key] = (
                has_spec,
                has_proof,
                is_external_body,
                line_number,
                link,
                function,
                module,
                "",
            )

    return results


def tweak_disambiguate(tracked: dict) -> dict:
    """
    Disambiguate tracked items that have the same qualified_name.

    Args:
        tracked: Dictionary mapping key to tuple of function metadata.

    Returns:
        New dictionary with disambiguated qualified_names in the tuples.
    """
    qualified_names = [value[5] for value in tracked.values()]
    name_counts = Counter(qualified_names)

    duplicates = {name for name, count in name_counts.items() if count > 1}

    if not duplicates:
        return tracked

    name_indices: dict[str, int] = {name: 0 for name in duplicates}

    new_tracked = {}
    for key, value in tracked.items():
        qualified_name = value[5]
        if qualified_name in duplicates:
            new_name = f"{qualified_name}_{name_indices[qualified_name]}"
            name_indices[qualified_name] += 1
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

    Args:
        tracked: Dictionary mapping key to tuple of function metadata.

    Returns:
        Dictionary mapping file_path (str) to dict with keys:
            - code-path: Relative path to source file
            - code-line: Line number where function starts
            - code-name: null (populated later by atomize)
    """
    result = {}

    for value in tracked.values():
        github_link, qualified_name = value[4], value[5]
        code_path, line_start = parse_github_link(github_link)

        if not code_path:
            continue

        func_name = qualified_name.replace('::', '.')
        file_path = f"{code_path}/{func_name}.md"

        result[file_path] = {
            "code-line": line_start,
            "code-path": code_path,
            "code-name": None,
        }

    return result


def _format_yaml_value(value) -> str:
    """Format a Python value as a YAML scalar."""
    if isinstance(value, dict):
        raise ValueError(f"Nested dicts are not supported in metadata: {value}")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        formatted_items = [_format_yaml_value(item) for item in value]
        return "[" + ", ".join(formatted_items) + "]"
    s = str(value)
    if (s in ("null", "true", "false", "~", "") or
        s.startswith(("{", "[", "'", '"', "|", ">", "*", "&", "!")) or
        ":" in s or "#" in s or "\n" in s):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return s


def generate_structure_files(structure: dict[str, dict], structure_root: Path) -> None:
    """
    Generate structure .md files from a structure dictionary.

    Args:
        structure: Dictionary mapping file_path to flat metadata dict.
        structure_root: Directory where structure files will be created.
    """
    created_count = 0

    for relative_path_str, metadata in structure.items():
        file_path = structure_root / relative_path_str
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists():
            print(f"WARNING: File already exists, overwriting: {file_path}")

        body_content = metadata.pop("content", None) if "content" in metadata else None

        yaml_lines = ["---"]
        for key, value in metadata.items():
            formatted_value = _format_yaml_value(value)
            yaml_lines.append(f"{key}: {formatted_value}")
        yaml_lines.append("---")
        yaml_lines.append("")

        if body_content:
            yaml_lines.append(body_content)
            yaml_lines.append("")

        file_content = "\n".join(yaml_lines)
        file_path.write_text(file_content, encoding='utf-8')
        created_count += 1

        if body_content is not None:
            metadata["content"] = body_content

    print(f"Created {created_count} structure files in {structure_root}")


def cmd_create(args: argparse.Namespace) -> None:
    """
    Execute the create subcommand.

    Initialize verilib structure files from various sources.
    """
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"
    structure_json_path = verilib_path / "stubs.json"

    if args.type == "blueprint":
        structure_root_relative = "blueprint"
        if args.root is not None:
            print("Warning: --root is ignored for blueprint type (fixed to 'blueprint')")

        if not check_leanblueprint_installed():
            print("Error: leanblueprint CLI is not installed or not in PATH", file=sys.stderr)
            print("Install it with: pip install leanblueprint", file=sys.stderr)
            sys.exit(1)

        run_leanblueprint_web(project_root)

        blueprint_json_path = verilib_path / "blueprint.json"
        blueprint_data = generate_blueprint_json(project_root, blueprint_json_path)

        structure = blueprint_to_structure(blueprint_data)

    elif args.type == "dalek-lite":
        if args.root is None:
            structure_root_relative = ".verilib/structure"
        else:
            structure_root_relative = str(args.root)

        tracked_path = project_root / "functions_to_track.csv"
        if not tracked_path.exists():
            print(f"Error: {tracked_path} not found", file=sys.stderr)
            sys.exit(1)

        tracked_output_path = verilib_path / "tracked_functions.csv"
        run_analyze_verus_specs_proofs(project_root, tracked_path, tracked_output_path)

        tracked = read_tracked_csv(tracked_output_path)
        tracked = tweak_disambiguate(tracked)
        structure = tracked_to_structure(tracked)

    else:
        print(f"Error: Unknown type '{args.type}'", file=sys.stderr)
        sys.exit(1)

    # Write config file
    config_path = verilib_path / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Create .gitignore for generated files
    gitignore_path = verilib_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_content = """# Generated by VeriLib (not tracked)
atoms.json
specs.json
stubs.json
proofs.json
"""
        gitignore_path.write_text(gitignore_content, encoding="utf-8")

    config = {
        "structure-type": args.type,
        "structure-root": structure_root_relative,
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote config to {config_path}")

    print("\nGenerating structure files...")
    generate_structure_files(structure, project_root / structure_root_relative)


# =============================================================================
# ATOMIZE SUBCOMMAND FUNCTIONS
# =============================================================================

def generate_probe_atoms(project_root: Path, atoms_path: Path) -> dict[str, dict]:
    """
    Run probe-verus atomize on the project and save results to atoms.json.

    Args:
        project_root: Root directory of the project to analyze.
        atoms_path: Path where atoms.json will be written.

    Returns:
        Dictionary mapping probe-name to atom data.

    Raises:
        SystemExit: If probe-verus is not installed or fails to run.
    """
    check_probe_verus_or_exit()

    atoms_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Running probe-verus atomize on {project_root}...")
    result = subprocess.run(
        ["probe-verus", "atomize", str(project_root), "-o", str(atoms_path), "-r"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: probe-verus atomize failed.")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(1)

    # Clean up generated intermediate files
    for cleanup_file in ["data/index.scip", "data/index.scip.json"]:
        cleanup_path = project_root / cleanup_file
        if cleanup_path.exists():
            cleanup_path.unlink()

    data_dir = project_root / "data"
    if data_dir.exists() and data_dir.is_dir() and not any(data_dir.iterdir()):
        data_dir.rmdir()

    print(f"Results saved to {atoms_path}")

    with open(atoms_path, encoding='utf-8') as f:
        return json.load(f)


def generate_probe_index(probe_atoms: dict[str, dict]) -> dict[str, IntervalTree]:
    """
    Build an interval tree index for fast line-based lookups.

    Args:
        probe_atoms: Dictionary mapping probe-name to atom data.

    Returns:
        Dictionary mapping code-path to IntervalTree.
    """
    trees: dict[str, IntervalTree] = defaultdict(IntervalTree)

    for probe_name, atom_data in probe_atoms.items():
        code_path = atom_data.get('code-path')
        code_text = atom_data.get('code-text', {})

        if not code_path:
            continue

        lines_start = code_text.get('lines-start')
        lines_end = code_text.get('lines-end')

        if lines_start is None or lines_end is None:
            continue

        interval_end = lines_end + 1
        trees[code_path].addi(lines_start, interval_end, probe_name)

    return dict(trees)


def filter_probe_atoms(probe_atoms: dict[str, dict], prefix: str) -> dict[str, dict]:
    """
    Filter probe atoms to only those where probe-name starts with prefix.

    Args:
        probe_atoms: Dictionary mapping probe-name to atom data
        prefix: Crate name to filter by

    Returns:
        Filtered dictionary
    """
    uri_prefix = f"probe:{prefix}/"
    return {
        probe_name: atom_data
        for probe_name, atom_data in probe_atoms.items()
        if probe_name.startswith(uri_prefix)
    }


def load_structure_from_files(structure_root: Path) -> dict[str, dict]:
    """
    Load structure from .md files into a dictionary.

    Args:
        structure_root: Path to the structure root directory.

    Returns:
        Dictionary mapping relative file path to frontmatter metadata.
    """
    structure = {}

    for md_file in structure_root.rglob("*.md"):
        post = frontmatter.load(md_file)
        relative_path = str(md_file.relative_to(structure_root))
        structure[relative_path] = dict(post.metadata)

    return structure


# Alias for blueprint structure loading (same logic, different frontmatter keys)
load_blueprint_structure_from_files = load_structure_from_files


def _update_entry_from_atoms(
    entry: dict,
    probe_index: dict[str, IntervalTree],
    probe_atoms: dict[str, dict],
    context: str = ""
) -> tuple[dict | None, str | None]:
    """
    Update a structure entry with probe atom data.

    Args:
        entry: Dictionary with 'code-path', 'code-line', and optionally 'code-name'
        probe_index: Dictionary mapping code-path to IntervalTree
        probe_atoms: Dictionary mapping probe-name to atom data
        context: Optional context string for warning messages

    Returns:
        Tuple of (updated_entry, error_message).
    """
    code_path = entry.get('code-path')
    line_start = entry.get('code-line')
    existing_probe_name = entry.get('code-name')

    updated = dict(entry)

    if existing_probe_name and existing_probe_name in probe_atoms:
        atom = probe_atoms[existing_probe_name]
        atom_code_path = atom.get('code-path')
        atom_code_text = atom.get('code-text', {})
        atom_line_start = atom_code_text.get('lines-start')

        if code_path != atom_code_path:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: code-path mismatch{ctx}: "
                  f"'{code_path}' will be overwritten with '{atom_code_path}'")

        if line_start != atom_line_start:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: code-line mismatch{ctx}: "
                  f"{line_start} will be overwritten with {atom_line_start}")

        updated['code-path'] = atom_code_path
        updated['code-line'] = atom_line_start

    else:
        if existing_probe_name:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: code-name '{existing_probe_name}' not found in probe_atoms{ctx}, "
                  f"looking up by code-path/code-line")
        if not code_path or line_start is None:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: Missing code-path or code-line{ctx}; code-name will not be generated")
            return updated, None

        if code_path not in probe_index:
            return None, f"code-path '{code_path}' not found in probe_index"

        tree = probe_index[code_path]
        matching_intervals = tree[line_start]
        exact_matches = [iv for iv in matching_intervals if iv.begin == line_start]

        if not exact_matches:
            return None, f"No interval starting at line {line_start} in {code_path}"

        if len(exact_matches) > 1:
            ctx = f" for {context}" if context else ""
            print(f"WARNING: Multiple intervals starting at line {line_start} in {code_path}{ctx}")

        interval = exact_matches[0]
        updated['code-name'] = interval.data

    return updated, None


def sync_structure_files_with_atoms(
    probe_index: dict[str, IntervalTree],
    probe_atoms: dict[str, dict],
    structure_root: Path
) -> None:
    """Sync structure .md files with probe atoms index."""
    updated_count = 0
    not_found_count = 0

    for md_file in structure_root.rglob("*.md"):
        post = frontmatter.load(md_file)
        entry = dict(post.metadata)

        updated, error = _update_entry_from_atoms(entry, probe_index, probe_atoms, str(md_file))

        if error:
            print(f"WARNING: {error} for {md_file}")
            not_found_count += 1
            continue

        post['code-path'] = updated['code-path']
        post['code-line'] = updated['code-line']
        if 'code-name' in updated:
            post['code-name'] = updated['code-name']

        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(frontmatter.dumps(post))

        updated_count += 1

    print(f"Structure files updated: {updated_count}")
    print(f"Not found/skipped: {not_found_count}")


def sync_structure_json_with_atoms(
    structure: dict[str, dict],
    probe_index: dict[str, IntervalTree],
    probe_atoms: dict[str, dict],
) -> dict[str, dict]:
    """Sync structure dictionary with probe atoms index."""
    updated_count = 0
    not_found_count = 0
    result = {}

    for file_path, entry in structure.items():
        updated, error = _update_entry_from_atoms(entry, probe_index, probe_atoms, file_path)

        if error:
            print(f"WARNING: {error} for {file_path}")
            not_found_count += 1
            result[file_path] = entry
            continue

        result[file_path] = updated
        updated_count += 1

    print(f"Structure entries updated: {updated_count}")
    print(f"Not found/skipped: {not_found_count}")

    return result


def _generate_enriched_entry(
    probe_name: str,
    probe_atoms: dict[str, dict],
) -> tuple[dict | None, str | None]:
    """Generate enriched entry from probe atom data.

    Returns a dict with code-path, code-lines, code-name, code-module, dependencies, display-name.
    """
    if not probe_name or probe_name not in probe_atoms:
        return None, "Missing or invalid code-name"

    atom = probe_atoms[probe_name]
    code_path = atom.get('code-path')
    code_text = atom.get('code-text', {})
    lines_start = code_text.get('lines-start')
    lines_end = code_text.get('lines-end')
    dependencies = atom.get('dependencies', [])

    if not code_path or lines_start is None or lines_end is None:
        return None, "Missing code-path or line info"

    code_module = atom.get('code-module', '')
    display_name = atom.get('display-name', '')

    enriched_entry = {
        "code-path": code_path,
        "code-lines": {
            "start": lines_start,
            "end": lines_end
        },
        "code-name": probe_name,
        "code-module": code_module,
        "dependencies": dependencies,
        "display-name": display_name,
    }

    return enriched_entry, None


def enrich_structure_json(
    structure: dict[str, dict],
    probe_atoms: dict[str, dict],
) -> dict[str, dict]:
    """Enrich structure JSON with atom metadata.

    Keys are file paths, values are enriched entries with code-path, code-lines,
    code-name, code-module, dependencies, display-name.
    """
    result = {}
    enriched_count = 0
    skipped_count = 0

    for file_path, entry in structure.items():
        probe_name = entry.get('code-name')

        enriched_entry, error = _generate_enriched_entry(probe_name, probe_atoms)

        if error:
            print(f"WARNING: {error} for {file_path}")
            skipped_count += 1
            # Keep original entry if enrichment fails
            result[file_path] = entry
            continue

        result[file_path] = enriched_entry
        enriched_count += 1

    print(f"Entries enriched: {enriched_count}")
    print(f"Skipped: {skipped_count}")

    return result


def populate_blueprint_json_metadata(
    structure: dict[str, dict],
    blueprint_data: dict[str, dict],
) -> dict[str, dict]:
    """Generate metadata dictionary from blueprint structure JSON."""
    result = {}
    created_count = 0
    skipped_count = 0

    for file_path, entry in structure.items():
        veri_name = entry.get('veri-name')
        if not veri_name:
            print(f"WARNING: Missing veri-name for {file_path}")
            skipped_count += 1
            continue

        node_id = veri_name[5:] if veri_name.startswith("veri:") else veri_name

        if node_id not in blueprint_data:
            print(f"WARNING: Node '{node_id}' not found in blueprint.json for {file_path}")
            skipped_count += 1
            continue

        node_info = blueprint_data[node_id]

        type_deps = node_info.get('type-dependencies', [])
        term_deps = node_info.get('term-dependencies', [])
        all_deps = [f"veri:{dep}" for dep in type_deps + term_deps]

        meta_data = {
            "dependencies": all_deps,
        }

        result[veri_name] = meta_data
        created_count += 1

    print(f"Metadata entries created: {created_count}")
    print(f"Skipped: {skipped_count}")

    return result


def cmd_atomize(args: argparse.Namespace) -> None:
    """
    Execute the atomize subcommand.

    Update verilib structure by syncing with atoms.
    """
    project_root = args.project_root.resolve()
    update_stubs = args.update_stubs
    config = load_config(project_root)

    structure_type = config.get("structure-type")
    verilib_path = config["verilib_path"]
    structure_root = config["structure_root"]
    structure_json_path = config["structure_json_path"]

    if structure_type == "blueprint":
        blueprint_path = verilib_path / "blueprint.json"
        if not blueprint_path.exists():
            print(f"Error: {blueprint_path} not found. Run 'structure.py create' first.", file=sys.stderr)
            raise SystemExit(1)

        print(f"Loading blueprint from {blueprint_path}...")
        with open(blueprint_path, encoding='utf-8') as f:
            blueprint_data = json.load(f)

        # Load structure from .md files
        print(f"Loading structure from {structure_root}...")
        structure = load_blueprint_structure_from_files(structure_root)

        print("Populating structure metadata from blueprint...")
        metadata = populate_blueprint_json_metadata(structure, blueprint_data)

        print(f"Saving enriched structure to {structure_json_path}...")
        with open(structure_json_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        print("Done.")

    elif structure_type == "dalek-lite":
        atoms_path = verilib_path / "atoms.json"
        probe_atoms = generate_probe_atoms(project_root, atoms_path)
        probe_atoms = filter_probe_atoms(probe_atoms, PROBE_PREFIX)
        probe_index = generate_probe_index(probe_atoms)

        # Load structure from .md files
        print(f"Loading structure from {structure_root}...")
        structure = load_structure_from_files(structure_root)

        print("Syncing structure with probe atoms...")
        structure = sync_structure_json_with_atoms(structure, probe_index, probe_atoms)

        # Optionally update .md files with code-name
        if update_stubs:
            print("Updating structure files with code-names...")
            sync_structure_files_with_atoms(probe_index, probe_atoms, structure_root)

        print("Enriching structure with atom metadata...")
        enriched = enrich_structure_json(structure, probe_atoms)

        print(f"Saving enriched structure to {structure_json_path}...")
        with open(structure_json_path, 'w', encoding='utf-8') as f:
            json.dump(enriched, f, indent=2)
        print("Done.")

    else:
        print(f"Error: Unknown structure type '{structure_type}'", file=sys.stderr)
        raise SystemExit(1)


# =============================================================================
# SPECIFY SUBCOMMAND FUNCTIONS
# =============================================================================

def run_probe_specify(project_root: Path, specs_path: Path, atoms_path: Path) -> dict:
    """
    Run probe-verus specify and return the results.

    Args:
        project_root: Root directory of the project to analyze.
        specs_path: Path where specs.json will be written.
        atoms_path: Path to atoms.json for probe-name lookup.

    Returns:
        Dictionary of specification data from probe-verus.

    Raises:
        SystemExit: If probe-verus is not installed or fails to run.
    """
    check_probe_verus_or_exit()

    specs_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Running probe-verus specify on {project_root}...")
    result = subprocess.run(
        ["probe-verus", "specify", str(project_root),
         "-o", str(specs_path),
         "-a", str(atoms_path)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: probe-verus specify failed.")
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
        specs_data: Dictionary from probe-verus specify output.

    Returns:
        Dictionary mapping probe-name to spec data for functions with specs.
    """
    result = {}

    for probe_name, func_info in specs_data.items():
        has_requires = func_info.get('has_requires', False)
        has_ensures = func_info.get('has_ensures', False)

        if has_requires or has_ensures:
            result[probe_name] = func_info

    return result


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
            kind = func_info.get('kind', '?')
            type_status = func_info.get('type-status', '?')
            node_id = name[5:] if name.startswith("veri:") else name
            print(f"  [{i}] {node_id}")
            print(f"      Kind: {kind}, Status: {type_status}")
        else:
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

    selected = set()
    parts = user_input.replace(',', ' ').split()

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if '-' in part:
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
            try:
                idx = int(part) - 1
                if 0 <= idx < len(functions):
                    selected.add(idx)
                else:
                    print(f"Warning: {part} out of range, skipping")
            except ValueError:
                print(f"Warning: Invalid number '{part}', skipping")

    return sorted(selected)


def cmd_specify(args: argparse.Namespace) -> None:
    """
    Execute the specify subcommand.

    Check specification status and manage specification certs.
    """
    project_root = args.project_root.resolve()
    config = load_config(project_root)

    structure_type = config.get("structure-type")
    verilib_path = config["verilib_path"]
    structure_root = config["structure_root"]
    certs_dir = config["certs_specify_dir"]

    if structure_type == "blueprint":
        blueprint_path = verilib_path / "blueprint.json"
        functions_with_specs = get_blueprint_functions_with_specs(blueprint_path)
        print(f"\nFound {len(functions_with_specs)} functions with specs in blueprint")

        structure_names = get_structure_names(structure_type, structure_root)
        print(f"Found {len(structure_names)} functions in structure")

        functions_in_structure = {
            veri_name: spec_info
            for veri_name, spec_info in functions_with_specs.items()
            if veri_name in structure_names
        }
        print(f"Found {len(functions_in_structure)} functions with specs in structure")

    else:
        specs_path = verilib_path / "specs.json"
        atoms_path = verilib_path / "atoms.json"
        specs_data = run_probe_specify(project_root, specs_path, atoms_path)

        functions_with_specs = get_functions_with_specs(specs_data)
        print(f"\nFound {len(functions_with_specs)} functions with specs in codebase")

        structure_names = get_structure_names(structure_type, structure_root)
        print(f"Found {len(structure_names)} functions in structure")

        functions_in_structure = {
            probe_name: spec_info
            for probe_name, spec_info in functions_with_specs.items()
            if probe_name in structure_names
        }
        print(f"Found {len(functions_in_structure)} functions with specs in structure")

    existing_certs = get_existing_certs(certs_dir)
    print(f"Found {len(existing_certs)} existing certs")

    uncertified = {
        name: spec_info
        for name, spec_info in functions_in_structure.items()
        if name not in existing_certs
    }

    if not uncertified:
        print("\nAll functions with specs in structure are already validated!")
        return

    print(f"\n{len(uncertified)} functions with specs need certification")

    uncertified_list = sorted(uncertified.items(), key=lambda x: x[0])

    selected_indices = display_menu(uncertified_list, structure_type)

    if not selected_indices:
        print("\nNo functions selected.")
        return

    print(f"\nCreating certs for {len(selected_indices)} functions...")

    for idx in selected_indices:
        name, _ = uncertified_list[idx]
        cert_path = create_cert(certs_dir, name)
        print(f"  Created: {cert_path.name}")

    print(f"\nDone. Created {len(selected_indices)} cert files in {certs_dir}")


# =============================================================================
# VERIFY SUBCOMMAND FUNCTIONS
# =============================================================================

def run_probe_verify(
    project_root: Path,
    proofs_path: Path,
    atoms_path: Path,
    verify_only_module: str | None = None
) -> dict:
    """
    Run probe-verus verify and return the results.

    Args:
        project_root: Root directory of the project to analyze.
        proofs_path: Path where proofs.json will be written.
        atoms_path: Path to atoms.json for probe-name lookup.
        verify_only_module: Optional module name to verify only that module.

    Returns:
        Dictionary of verification data from probe-verus.

    Raises:
        SystemExit: If probe-verus is not installed or fails to run.
    """
    check_probe_verus_or_exit()

    proofs_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "probe-verus", "verify", str(project_root),
        "-o", str(proofs_path),
        "-a", str(atoms_path)
    ]
    if verify_only_module:
        cmd.extend(["--verify-only-module", verify_only_module])

    if verify_only_module:
        print(f"Running probe-verus verify on {project_root} (module: {verify_only_module})...")
    else:
        print(f"Running probe-verus verify on {project_root}...")

    result = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Error: probe-verus verify failed.")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(1)

    print(f"Verification results saved to {proofs_path}")

    # Clean up generated intermediate files
    for cleanup_file in ["data/verification_config.json", "data/verification_output.txt"]:
        cleanup_path = project_root / cleanup_file
        if cleanup_path.exists():
            cleanup_path.unlink()

    data_dir = project_root / "data"
    if data_dir.exists() and data_dir.is_dir() and not any(data_dir.iterdir()):
        data_dir.rmdir()

    with open(proofs_path, encoding='utf-8') as f:
        return json.load(f)


def get_verification_results(proofs_data: dict) -> tuple[set[str], set[str]]:
    """
    Extract verified and failed function probe-names from proofs data.

    The proofs.json schema from probe-verus is a dictionary keyed by probe-name:
    {
        "probe:crate/version/module/function()": {
            "code-path": "string",
            "code-line": number,
            "verified": boolean,
            "status": "success|failure|sorries|warning"
        }
    }

    Args:
        proofs_data: Dictionary from probe-verus verify output.

    Returns:
        Tuple of (verified_probe_names, failed_probe_names).
    """
    verified = set()
    failed = set()

    for probe_name, func_data in proofs_data.items():
        is_verified = func_data.get('verified', False)
        if is_verified:
            verified.add(probe_name)
        else:
            failed.add(probe_name)

    return verified, failed


def get_blueprint_verification_results(blueprint_path: Path) -> tuple[set[str], set[str]]:
    """
    Extract verified and failed veri-names from blueprint.json based on term-status.

    Only 'fully-proved' term-status is considered verified.

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


def delete_cert(certs_dir: Path, name: str) -> Path | None:
    """
    Delete a cert file for a function.

    Args:
        certs_dir: Path to the certs directory.
        name: The identifier (code-name or veri-name).

    Returns:
        Path to the deleted cert file, or None if it didn't exist.
    """
    encoded_name = encode_name(name)
    cert_path = certs_dir / f"{encoded_name}.json"

    if cert_path.exists():
        cert_path.unlink()
        return cert_path

    return None


def cmd_verify(args: argparse.Namespace) -> None:
    """
    Execute the verify subcommand.

    Run verification and manage verification certs.
    """
    project_root = args.project_root.resolve()
    config = load_config(project_root)

    structure_type = config.get("structure-type")
    verilib_path = config["verilib_path"]
    structure_root = config["structure_root"]
    certs_dir = config["certs_verify_dir"]

    if structure_type == "blueprint":
        if args.verify_only_module:
            print("Warning: --verify-only-module is ignored for blueprint type")

        blueprint_path = verilib_path / "blueprint.json"
        verified_funcs, failed_funcs = get_blueprint_verification_results(blueprint_path)

    elif structure_type == "dalek-lite":
        proofs_path = verilib_path / "proofs.json"
        atoms_path = verilib_path / "atoms.json"
        proofs_data = run_probe_verify(
            project_root, proofs_path, atoms_path,
            verify_only_module=args.verify_only_module
        )
        verified_funcs, failed_funcs = get_verification_results(proofs_data)

    else:
        print(f"Error: Unknown structure type '{structure_type}'", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nVerification summary:")
    print(f"  Verified: {len(verified_funcs)}")
    print(f"  Failed: {len(failed_funcs)}")

    structure_names = get_structure_names(structure_type, structure_root)
    print(f"  Functions in structure: {len(structure_names)}")

    verified_in_structure = verified_funcs & structure_names
    failed_in_structure = failed_funcs & structure_names
    print(f"  Verified in structure: {len(verified_in_structure)}")
    print(f"  Failed in structure: {len(failed_in_structure)}")

    existing_certs = get_existing_certs(certs_dir)
    print(f"  Existing certs: {len(existing_certs)}")

    to_create = verified_in_structure - existing_certs
    to_delete = failed_in_structure & existing_certs

    created = []
    deleted = []

    for name in sorted(to_create):
        cert_path = create_cert(certs_dir, name)
        created.append((name, cert_path))

    for name in sorted(to_delete):
        cert_path = delete_cert(certs_dir, name)
        if cert_path:
            deleted.append((name, cert_path))

    def get_display_name(name: str) -> str:
        if name.startswith("veri:"):
            return name[5:]
        else:
            return name.split('#')[-1].rstrip('()')

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

    print("\n" + "=" * 60)
    final_certs = len(existing_certs) + len(created) - len(deleted)
    print(f"Total certs: {len(existing_certs)} → {final_certs}")
    print(f"  Created: +{len(created)}")
    print(f"  Deleted: -{len(deleted)}")
    print("=" * 60)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main() -> None:
    """Main entry point with subcommand routing."""
    parser = argparse.ArgumentParser(
        description="Unified CLI for verilib structure management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  create   Initialize structure files from source analysis
  atomize  Enrich structure files with metadata
  specify  Check specification status and manage spec certs
  verify   Run verification and manage verification certs

Examples:
  uv run scripts/structure.py create --type dalek-lite
  uv run scripts/structure.py create --type blueprint
  uv run scripts/structure.py atomize
  uv run scripts/structure.py specify
  uv run scripts/structure.py verify --verify-only-module edwards
"""
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Create subcommand
    create_parser = subparsers.add_parser(
        "create",
        help="Initialize structure files from source analysis"
    )
    create_parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    create_parser.add_argument(
        "--type",
        choices=["dalek-lite", "blueprint"],
        required=True,
        help="Type of the source to analyze"
    )
    create_parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Root directory for structure files (default: .verilib/structure)"
    )

    # Atomize subcommand
    atomize_parser = subparsers.add_parser(
        "atomize",
        help="Enrich structure files with metadata"
    )
    atomize_parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    atomize_parser.add_argument(
        "-s", "--update-stubs",
        action="store_true",
        dest="update_stubs",
        help="Update structure files (.md or stubs.json) with code-name from atoms"
    )

    # Specify subcommand
    specify_parser = subparsers.add_parser(
        "specify",
        help="Check specification status and manage spec certs"
    )
    specify_parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )

    # Verify subcommand
    verify_parser = subparsers.add_parser(
        "verify",
        help="Run verification and manage verification certs"
    )
    verify_parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project root directory (default: current working directory)"
    )
    verify_parser.add_argument(
        "--verify-only-module",
        type=str,
        default=None,
        help="Only verify functions in this module (dalek-lite only)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "create":
        cmd_create(args)
    elif args.command == "atomize":
        cmd_atomize(args)
    elif args.command == "specify":
        cmd_specify(args)
    elif args.command == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
