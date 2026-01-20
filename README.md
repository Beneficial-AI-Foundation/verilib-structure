# verilib-structure

Verilib structure files for outlining verification goals

## Installation

### Prerequisites

1. Install dependencies for Blueprint (if using blueprint type).
   ```
   apt install libgraphviz-dev graphviz
   pip install leanblueprint
   ```

2. Install proof tools: Verus, Verus Analyzer, SCIP (if using dalek-lite type).
   ```
   git clone https://github.com/Beneficial-AI-Foundation/installers_for_various_tools
   cd installers_for_various_tools
   python3 verus_installer_from_release.py --version "0.2025.08.25.63ab0cb"
   python3 verus_analyzer_installer.py
   python3 scip_installer.py
   ```

3. Install atomization and verification tool (if using dalek-lite type).
   ```
   git clone https://github.com/Beneficial-AI-Foundation/probe-verus
   cd probe-verus
   cargo install --path .
   ```

### Install verilib-structure

**From source:**

```bash
git clone git@github.com:Beneficial-AI-Foundation/verilib-structure.git
cd verilib-structure
cargo install --path .
```

This installs the `verilib-structure` binary to `~/.cargo/bin/`.

**Development build:**

```bash
cargo build --release
# Binary available at ./target/release/verilib-structure
```

## Usage

```bash
verilib-structure <command> [options]
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
verilib-structure create [PROJECT_ROOT] --type <type> [--form <form>] [--root <root>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_ROOT` | Project root directory (default: current working directory) |

**Options:**

| Option | Values | Description |
|--------|--------|-------------|
| `--type` | `dalek-lite`, `blueprint` | Type of the source to analyze (required) |
| `--form` | `json`, `files` | Structure form (default: `json`) |
| `--root` | path | Root directory for structure files (default: `.verilib/structure`, ignored for blueprint which uses `blueprint`) |

**Structure forms:**

- `json`: Writes structure dictionary to `<project_root>/.verilib/structure_files.json`
- `files`: Creates a hierarchy of `.md` files under the root directory

**Config file:**

Creates `<project_root>/.verilib/config.json` with:
```json
{
  "structure-type": "dalek-lite",
  "structure-form": "files",
  "structure-root": ".verilib/structure"
}
```

**Examples:**

```bash
# Dalek-lite: Generate JSON structure file
verilib-structure create --type dalek-lite --form json

# Dalek-lite: Generate .md file hierarchy
verilib-structure create --type dalek-lite --form files

# Blueprint: Generate JSON structure file (default)
verilib-structure create --type blueprint

# Blueprint: Generate .md file hierarchy
verilib-structure create --type blueprint --form files
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

- **dalek-lite**: Runs `probe-verus atomize` to generate atom data, syncs structure with `scip-name` identifiers
- **blueprint**: Reads `blueprint.json` to generate metadata with `veri-name` and dependencies

**Note:** Requires `config.json` created by `create`. The type and form are read from `structure-type` and `structure-form` fields in the config file.

**Usage:**

```bash
verilib-structure atomize [PROJECT_ROOT]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_ROOT` | Project root directory (default: current working directory) |

**Structure forms (from config):**

- `json`: Generates `<project_root>/.verilib/structure_meta.json` with metadata
- `files`: Generates companion `.meta.verilib` and `.atom.verilib` files

**Examples:**

```bash
# Update structure and generate metadata (current directory)
verilib-structure atomize

# Update structure for a specific project
verilib-structure atomize /path/to/project
```

**Generated metadata format (dalek-lite):**

The `structure_meta.json` file maps probe-name to metadata:

```json
{
  "probe:curve25519-dalek/4.1.3/montgomery/MontgomeryPoint#ct_eq()": {
    "code-path": "curve25519-dalek/src/montgomery.rs",
    "code-lines": { "start": 42, "end": 50 },
    "code-module": "montgomery",
    "dependencies": ["probe:..."],
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

- **dalek-lite**: Runs `probe-verus specify` to identify functions with `requires`/`ensures` specs
- **blueprint**: Checks `type-status` in `blueprint.json` (`stated` or `mathlib` = has spec)

**Usage:**

```bash
verilib-structure specify [PROJECT_ROOT]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_ROOT` | Project root directory (default: current working directory) |

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
verilib-structure specify

# Check and certify specs for a specific project
verilib-structure specify /path/to/project
```

**Interactive selection:**

When prompted, you can enter:
- Individual numbers: `1, 3, 5`
- Ranges: `1-5`
- `all` to select all uncertified functions
- `none` or empty to skip

### verify

Runs verification and automatically manages verification certs. Behavior depends on structure type:

- **dalek-lite**: Runs `probe-verus verify` to check proof status
- **blueprint**: Checks `term-status` in `blueprint.json` (`fully-proved` = verified)

**Usage:**

```bash
verilib-structure verify [PROJECT_ROOT] [--verify-only-module <module>]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_ROOT` | Project root directory (default: current working directory) |

**Options:**

| Option | Description |
|--------|-------------|
| `--verify-only-module` | Only verify functions in this module (dalek-lite only) |

**Workflow:**

1. Checks verification status (dalek-lite: probe-verus, blueprint: term-status)
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
verilib-structure verify

# Run verification for a specific project
verilib-structure verify /path/to/project

# Run verification for only the edwards module (dalek-lite only)
verilib-structure verify --verify-only-module edwards
```

**Output:**

The command shows a summary of changes:
```
============================================================
VERIFICATION CERT CHANGES
============================================================

✓ Created 5 new certs:
  + func_name
    probe:crate/version/module#func()

✗ Deleted 2 certs (verification failed):
  - other_func
    probe:crate/version/module#other_func()

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
   verilib-structure create --type dalek-lite --form files
   ```

2. Run atomization checks
   ```
   verilib-structure atomize
   ```

3. Run specification checks
   ```
   verilib-structure specify
   ```

4. Run verification checks
   ```
   verilib-structure verify
   ```

### Blueprint (Equational Theories)

1. Create structure files
   ```
   git clone git@github.com:Beneficial-AI-Foundation/equational_theories.git
   cd equational_theories
   verilib-structure create --type blueprint
   ```

2. Run atomization checks
   ```
   verilib-structure atomize
   ```

3. Run specification checks
   ```
   verilib-structure specify
   ```

4. Run verification checks
   ```
   verilib-structure verify
   ```

## Building from Source

**Requirements:**
- Rust 1.70 or later
- OpenSSL development libraries (`libssl-dev` on Ubuntu/Debian)

**Build commands:**

```bash
# Debug build
cargo build

# Release build (optimized)
cargo build --release

# Install to ~/.cargo/bin
cargo install --path .

# Run tests
cargo test

# Run with cargo (without installing)
cargo run -- --help
cargo run -- create --type blueprint
```

## License

MIT
