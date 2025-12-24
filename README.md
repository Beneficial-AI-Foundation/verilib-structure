# verilib-structure
Verilib structure files for outlining verification goals

## Installation

1. Install dependencies for Blueprint.
   ```
   apt install libgraphviz-dev graphviz
   ```

2. Install proof tools: Verus, Verus Analyzer, SCIP.
   ```
   git clone https://github.com/Beneficial-AI-Foundation/installers_for_various_tools
   cd installers_for_various_tools
   python3 verus_installer_from_release.py --version "0.2025.08.25.63ab0cb"
   python3 verus_analyzer_installer.py
   python3 scip_installer.py
   ```

3. Install atomization and verification tool.
   ```
   git clone https://github.com/Beneficial-AI-Foundation/scip-atoms
   cd scip-atoms
   cargo install --path .
   ```

4. Install Structure.
   ```
   git clone git@github.com:Beneficial-AI-Foundation/verilib-structure.git
   export VERILIB_STRUCTURE_PATH=$(pwd)/verilib-structure
   ```

## Usage

The main script `structure.py` provides four subcommands:

```bash
uv run scripts/structure.py <command> [options]
```

| Command | Description |
|---------|-------------|
| `create` | Initialize structure files from source analysis |
| `atomize` | Enrich structure files with metadata |
| `specify` | Check specification status and manage spec certs |
| `verify` | Run verification and manage verification certs |

### create

Generates structure files from source analysis. Supports two structure types:

- **dalek-lite**: Analyzes Verus/Rust code via `analyze_verus_specs_proofs.py`
- **blueprint**: Runs `leanblueprint web` and parses dependency graph

**Usage:**

```bash
uv run scripts/structure.py create [project_root] --type <type> [--form <form>] [--root <root>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Options:**

| Option | Values | Description |
|--------|--------|-------------|
| `--type` | `dalek-lite`, `blueprint` | Type of the source to analyze (required) |
| `--form` | `json`, `files` | Structure form (default: `json`) |
| `--root` | path | Root directory for structure files (default: `.verilib`, ignored for blueprint which uses `blueprint`) |

**Structure forms:**

- `json`: Writes structure dictionary to `<project_root>/.verilib/structure_files.json`
- `files`: Creates a hierarchy of `.md` files under the root directory

**Config file:**

Creates `<project_root>/.verilib/config.json` with:
```json
{
  "structure-type": "dalek-lite",
  "structure-form": "files",
  "structure-root": ".verilib"
}
```

**Examples:**

```bash
# Dalek-lite: Generate JSON structure file
uv run scripts/structure.py create --type dalek-lite --form json

# Dalek-lite: Generate .md file hierarchy
uv run scripts/structure.py create --type dalek-lite --form files

# Blueprint: Generate JSON structure file (default)
uv run scripts/structure.py create --type blueprint

# Blueprint: Generate .md file hierarchy
uv run scripts/structure.py create --type blueprint --form files
```

**Generated file format (dalek-lite):**

```yaml
---
code-path: path/to/source/file.rs
code-line: 42
scip-name: null
---
```

**Generated file format (blueprint):**

```yaml
---
veri-name: veri:node_id
dependencies: [veri:dep1, veri:dep2]
---
<content from blueprint>
```

### atomize

Enriches structure files with metadata. Behavior depends on structure type:

- **dalek-lite**: Runs `scip-atoms` to generate SCIP data, syncs structure with `scip-name` identifiers
- **blueprint**: Reads `blueprint.json` to generate metadata with `veri-name` and dependencies

**Note:** Requires `config.json` created by `create`. The type and form are read from `structure-type` and `structure-form` fields in the config file.

**Usage:**

```bash
uv run scripts/structure.py atomize [project_root]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Structure forms (from config):**

- `json`: Generates `<project_root>/.verilib/structure_meta.json` with metadata
- `files`: Generates companion `.meta.verilib` and `.atom.verilib` files

**Examples:**

```bash
# Update structure and generate metadata (current directory)
uv run scripts/structure.py atomize

# Update structure for a specific project
uv run scripts/structure.py atomize /path/to/project
```

**Generated metadata format (dalek-lite):**

The `structure_meta.json` file maps scip-name to metadata:

```json
{
  "scip:curve25519-dalek/4.1.3/montgomery/MontgomeryPoint#ct_eq()": {
    "code-path": "curve25519-dalek/src/montgomery.rs",
    "code-lines": { "start": 42, "end": 50 },
    "code-module": "montgomery",
    "dependencies": ["scip:..."],
    "specified": false,
    "visible": true
  }
}
```

**Generated metadata format (blueprint):**

```json
{
  "veri:node_id": {
    "dependencies": ["veri:dep1", "veri:dep2"],
    "visible": true
  }
}
```

**Generated files (files form):**

For each `XXX.md` file, creates:

- `XXX.meta.verilib`: JSON metadata
- `XXX.atom.verilib`: Source code (dalek-lite) or content from blueprint.json (blueprint)

### specify

Checks specification status and manages specification certs. Behavior depends on structure type:

- **dalek-lite**: Runs `scip-atoms specify` to identify functions with `requires`/`ensures` specs
- **blueprint**: Checks `type-status` in `blueprint.json` (`stated` or `mathlib` = has spec)

**Usage:**

```bash
uv run scripts/structure.py specify [project_root]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Workflow:**

1. Identifies functions with specs (dalek-lite: requires/ensures, blueprint: type-status)
2. Compares with existing certs in `.verilib/certs/specify/`
3. Displays a multiple choice menu of uncertified functions
4. Creates cert files for user-selected functions

**Blueprint type-status mapping:**

| type-status | Has Spec? |
|-------------|-----------|
| `stated` | Yes |
| `mathlib` | Yes |
| `can-state` | No |
| `not-ready` | No |

**Cert files:**

Certs are stored in `.verilib/certs/specify/` with one JSON file per certified function:
- Filename: URL-encoded identifier (scip-name or veri-name) + `.json`
- Content: `{"timestamp": "<ISO 8601 timestamp>"}`

**Examples:**

```bash
# Check and certify specs (current directory)
uv run scripts/structure.py specify

# Check and certify specs for a specific project
uv run scripts/structure.py specify /path/to/project
```

**Interactive selection:**

When prompted, you can enter:
- Individual numbers: `1, 3, 5`
- Ranges: `1-5`
- `all` to select all uncertified functions
- `none` or empty to skip

### verify

Runs verification and automatically manages verification certs. Behavior depends on structure type:

- **dalek-lite**: Runs `scip-atoms verify` to check proof status
- **blueprint**: Checks `term-status` in `blueprint.json` (`fully-proved` = verified)

**Usage:**

```bash
uv run scripts/structure.py verify [project_root] [--verify-only-module <module>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Options:**

| Option | Description |
|--------|-------------|
| `--verify-only-module` | Only verify functions in this module (dalek-lite only) |

**Workflow:**

1. Checks verification status (dalek-lite: scip-atoms, blueprint: term-status)
2. Filters to only functions in the structure
3. Compares with existing certs in `.verilib/certs/verify/`
4. Creates new certs for verified functions without certs
5. Deletes certs for failed functions with existing certs
6. Shows summary of all changes

**Blueprint term-status mapping:**

| term-status | Verified? |
|-------------|-----------|
| `fully-proved` | Yes |
| `proved` | No |
| `defined` | No |
| `can-prove` | No |

**Cert files:**

Certs are stored in `.verilib/certs/verify/` with one JSON file per verified function:
- Filename: URL-encoded identifier (scip-name or veri-name) + `.json`
- Content: `{"timestamp": "<ISO 8601 timestamp>"}`

**Examples:**

```bash
# Run verification and update certs (current directory)
uv run scripts/structure.py verify

# Run verification for a specific project
uv run scripts/structure.py verify /path/to/project

# Run verification for only the edwards module (dalek-lite only)
uv run scripts/structure.py verify --verify-only-module edwards
```

**Output:**

The script shows a summary of changes:
```
============================================================
VERIFICATION CERT CHANGES
============================================================

✓ Created 5 new certs:
  + func_name
    scip:crate/version/module#func()

✗ Deleted 2 certs (verification failed):
  - other_func
    scip:crate/version/module#other_func()

============================================================
Total certs: 10 → 13
  Created: +5
  Deleted: -2
============================================================
```

## Case Studies


### Dalek-Lite

1. Create structure files
   ```
   git clone git@github.com:Beneficial-AI-Foundation/dalek-lite.git
   cd dalek-lite
   git checkout -b sl/structure
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py create --type dalek-lite --form files
   ```

2. Run atomization checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py atomize
   ```

3. Run specification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py specify
   ```

4. Run verification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py verify
   ```

### Blueprint (Equational Theories)

1. Create structure files
   ```
   git clone git@github.com:Beneficial-AI-Foundation/equational_theories.git
   cd equational_theories
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py create --type blueprint
   ```

2. Run atomization checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py atomize
   ```

3. Run specification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py specify
   ```

4. Run verification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure.py verify
   ```
