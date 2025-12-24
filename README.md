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

## Scripts

### structure_create.py

Generates structure files from tracked functions. Analyzes source code to identify functions and creates `.md` files with YAML frontmatter containing metadata.

**Usage:**

```bash
uv run scripts/structure_create.py [project_root] --type <type> --form <form> [--root <root>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Options:**

| Option | Values | Description |
|--------|--------|-------------|
| `--type` | `dalek-lite` | Type of the source to analyze (required) |
| `--form` | `json`, `files` | Structure form (required) |
| `--root` | path | Root directory for structure files, relative to project root (default: `.verilib`) |

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
# Generate JSON structure file (current directory)
uv run scripts/structure_create.py --type dalek-lite --form json

# Generate JSON structure file for a specific project
uv run scripts/structure_create.py /path/to/project --type dalek-lite --form json

# Generate .md file hierarchy in default location (.verilib/)
uv run scripts/structure_create.py --type dalek-lite --form files

# Generate .md file hierarchy in custom location (relative to project root)
uv run scripts/structure_create.py --type dalek-lite --form files --root my-structure
```

**Generated file format:**

Each `.md` file contains YAML frontmatter with:

```yaml
---
code-path: path/to/source/file.rs
code-line: 42
scip-name: null
---
```

### structure_atomize.py

Updates structure files by syncing with SCIP atoms. Runs `scip-atoms` to generate source code intelligence data, then updates the structure with `scip-name` identifiers and populates metadata.

**Note:** Requires `config.json` created by `structure_create.py`. The type and form are read from `structure-type` and `structure-form` fields in the config file.

**Usage:**

```bash
uv run scripts/structure_atomize.py [project_root]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Structure forms (from config):**

- `json`: Updates `<project_root>/.verilib/structure_files.json` with scip-names and generates `<project_root>/.verilib/structure_meta.json` with metadata
- `files`: Updates `.md` files with scip-names and generates companion `.meta.verilib` and `.atom.verilib` files

**Examples:**

```bash
# Update structure and generate metadata (current directory)
uv run scripts/structure_atomize.py

# Update structure for a specific project
uv run scripts/structure_atomize.py /path/to/project
```

**Generated metadata format (JSON):**

The `structure_meta.json` file maps scip-name to metadata:

```json
{
  "scip:curve25519-dalek/4.1.3/montgomery/MontgomeryPoint#ct_eq()": {
    "code-path": "curve25519-dalek/src/montgomery.rs",
    "code-lines": { "start": 42, "end": 50 },
    "code-module": "montgomery",
    "dependencies": ["..."],
    "specified": false,
    "visible": true
  }
}
```

**Generated metadata format (files):**

For each `XXX.md` file, creates:

- `XXX.meta.verilib`: JSON metadata (same fields as above, plus `scip-name`)
- `XXX.atom.verilib`: Raw source code extracted from the original file

### structure_specify.py

Checks specification status of functions and manages specification certs. Runs `scip-atoms specify` to identify functions with specs (requires/ensures), compares with existing certs, and lets users validate uncertified functions.

**Usage:**

```bash
uv run scripts/structure_specify.py [project_root]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Workflow:**

1. Runs `scip-atoms specify` to check which functions have `requires` or `ensures` specs
2. Compares with existing certs in `.verilib/certs/specify/`
3. Displays a multiple choice menu of uncertified functions
4. Creates cert files for user-selected functions

**Cert files:**

Certs are stored in `.verilib/certs/specify/` with one JSON file per certified function:
- Filename: URL-encoded scip-name + `.json`
- Content: `{"timestamp": "<ISO 8601 timestamp>"}`

**Examples:**

```bash
# Check and certify specs (current directory)
uv run scripts/structure_specify.py

# Check and certify specs for a specific project
uv run scripts/structure_specify.py /path/to/project
```

**Interactive selection:**

When prompted, you can enter:
- Individual numbers: `1, 3, 5`
- Ranges: `1-5`
- `all` to select all uncertified functions
- `none` or empty to skip

### structure_verify.py

Runs verification and automatically manages verification certs. Creates certs for newly verified functions and deletes certs for functions that now fail verification.

**Usage:**

```bash
uv run scripts/structure_verify.py [project_root] [--verify-only-module <module>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project_root` | Project root directory (default: current working directory) |

**Options:**

| Option | Description |
|--------|-------------|
| `--verify-only-module` | Only verify functions in this module (e.g., `edwards`) |

**Workflow:**

1. Runs `scip-atoms verify` to check which functions pass verification
2. Filters to only functions in the structure
3. Compares with existing certs in `.verilib/certs/verify/`
4. Creates new certs for verified functions without certs
5. Deletes certs for failed functions with existing certs
6. Shows summary of all changes

**Cert files:**

Certs are stored in `.verilib/certs/verify/` with one JSON file per verified function:
- Filename: URL-encoded scip-name + `.json`
- Content: `{"timestamp": "<ISO 8601 timestamp>"}`

**Examples:**

```bash
# Run verification and update certs (current directory)
uv run scripts/structure_verify.py

# Run verification for a specific project
uv run scripts/structure_verify.py /path/to/project

# Run verification for only the edwards module
uv run scripts/structure_verify.py --verify-only-module edwards
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
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_create.py --type dalek-lite --form files
   ```

2. Run atomization checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_atomize.py
   ```

3. Run specification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_specify.py
   ```

4. Run verification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_verify.py
   ```

### Dalek-Lite

1. Create structure files
   ```
   git clone git@github.com:Beneficial-AI-Foundation/equational_theories.git
   cd equational_theories
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_create.py --type blueprint --form json
   ```

2. Run atomization checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_atomize.py
   ```

3. Run specification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_specify.py
   ```

4. Run verification checks
   ```
   uv run $VERILIB_STRUCTURE_PATH/scripts/structure_verify.py
   ```
