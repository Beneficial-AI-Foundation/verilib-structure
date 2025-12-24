#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beartype",
#   "leanblueprint",
#   "lxml",
# ]
# ///
"""
Initialize verilib structure files from various sources.

For dalek-lite type:
    Analyzes source code using analyze_verus_specs_proofs to identify tracked functions,
    then generates .md structure files with YAML frontmatter containing code-path,
    code-line, and scip-name fields.

For blueprint type:
    Runs leanblueprint web to generate dependency graph, then creates structure files
    with scip-name (scip:<blueprint-id>) and content fields.

Usage:
    uv run scripts/structure_create.py --type dalek-lite --form files --root .verilib
    uv run scripts/structure_create.py --type dalek-lite --form json
    uv run scripts/structure_create.py --type blueprint --form json
    uv run scripts/structure_create.py --type blueprint --form files
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from lxml import html

from analyze_verus_specs_proofs import analyze_functions


# =============================================================================
# Blueprint dependency graph extraction functions (from extract_dep_graph.py)
# =============================================================================

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

    Creates a dictionary mapping file paths to metadata dictionaries for each
    node in the blueprint data. Does not include code-path or code-line fields.

    Args:
        blueprint_data: Dictionary mapping blueprint IDs to node attributes.

    Returns:
        Dictionary mapping file_path (str) to dict with keys:
            - scip-name: "scip:<blueprint-id>"
            - content: Content from blueprint data
    """
    result = {}
    for blueprint_id, attributes in blueprint_data.items():
        file_path = f"{blueprint_id}.md"
        result[file_path] = {
            "scip-name": f"scip:{blueprint_id}",
            "content": attributes.get("content", ""),
        }
    return result


# =============================================================================
# Dalek-lite functions
# =============================================================================

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

    If metadata contains a 'content' field, it is added below the frontmatter
    as markdown body text (not in the YAML).

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

        # Extract content field (if present) to add below frontmatter
        body_content = metadata.pop("content", None) if "content" in metadata else None

        # Build YAML frontmatter from metadata
        yaml_lines = ["---"]
        for key, value in metadata.items():
            formatted_value = _format_yaml_value(value)
            yaml_lines.append(f"{key}: {formatted_value}")
        yaml_lines.append("---")
        yaml_lines.append("")

        # Add body content if present
        if body_content:
            yaml_lines.append(body_content)
            yaml_lines.append("")

        file_content = "\n".join(yaml_lines)
        file_path.write_text(file_content, encoding='utf-8')
        created_count += 1

        # Restore content to metadata (in case it's used elsewhere)
        if body_content is not None:
            metadata["content"] = body_content

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
        choices=["dalek-lite", "blueprint"],
        required=True,
        help="Type of the source to analyze (blueprint uses fixed 'blueprint' root)"
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
    Initialize verilib structure files from various sources.

    Steps:
        1. Parse command line arguments
        2. Analyze source (dalek-lite: functions_to_track.csv, blueprint: leanblueprint)
        3. Generate structure output (JSON or files)
    """
    args = parse_args()

    # Resolve project root to absolute path
    project_root = args.project_root.resolve()
    verilib_path = project_root / ".verilib"
    structure_json_path = verilib_path / "structure_files.json"

    if args.type == "blueprint":
        # Blueprint type: fixed root at "blueprint", cannot be changed
        structure_root_relative = "blueprint"
        if args.root is not None:
            print("Warning: --root is ignored for blueprint type (fixed to 'blueprint')")

        # Check if leanblueprint CLI is installed
        if not check_leanblueprint_installed():
            print("Error: leanblueprint CLI is not installed or not in PATH", file=sys.stderr)
            print("Install it with: pip install leanblueprint", file=sys.stderr)
            sys.exit(1)

        # Run leanblueprint web to generate blueprint/web
        run_leanblueprint_web(project_root)

        # Generate blueprint.json from the web files
        blueprint_json_path = verilib_path / "blueprint.json"
        blueprint_data = generate_blueprint_json(project_root, blueprint_json_path)

        # Convert blueprint data to structure format
        structure = blueprint_to_structure(blueprint_data)

    elif args.type == "dalek-lite":
        # Dalek-lite type: uses functions_to_track.csv
        if args.root is None:
            structure_root_relative = ".verilib"
        else:
            structure_root_relative = str(args.root)

        tracked_path = project_root / "functions_to_track.csv"
        if not tracked_path.exists():
            print(f"Error: {tracked_path} not found", file=sys.stderr)
            sys.exit(1)

        print("Analyzing source code to derive list of functions to track...")
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
