//! Atomize subcommand implementation.
//!
//! Enrich structure files with metadata from SCIP atoms.

use crate::config::constants::PROBE_PREFIX;
use crate::config::ConfigPaths;
use crate::utils::{check_probe_verus_or_exit, parse_frontmatter, run_command};
use anyhow::{bail, Context, Result};
use intervaltree::IntervalTree;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Run the atomize subcommand.
pub fn run(project_root: PathBuf, update_stubs: bool) -> Result<()> {
    let project_root = project_root
        .canonicalize()
        .context("Failed to resolve project root")?;
    let config = ConfigPaths::load(&project_root)?;

    // Step 1: Generate stubs.json from .md files using probe-verus stubify
    let stubs = generate_stubs(&config.structure_root, &config.structure_json_path)?;
    println!("Loaded {} stubs from structure files", stubs.len());

    // Step 2: Generate atoms.json using probe-verus atomize
    let probe_atoms = generate_probe_atoms(&project_root, &config.atoms_path)?;
    let probe_atoms = filter_probe_atoms(&probe_atoms, PROBE_PREFIX);
    println!("Loaded {} filtered atoms", probe_atoms.len());

    // Step 3: Build probe index for fast lookups
    let probe_index = generate_probe_index(&probe_atoms);

    // Step 4: Enrich stubs with code-name and all atom metadata
    println!("Enriching stubs with atom metadata...");
    let enriched = enrich_stubs_with_atoms(stubs, &probe_index, &probe_atoms)?;

    // Step 5: Save enriched stubs.json
    println!(
        "Saving enriched stubs to {}...",
        config.structure_json_path.display()
    );
    let content = serde_json::to_string_pretty(&enriched)?;
    std::fs::write(&config.structure_json_path, content)?;

    // Optionally update .md files with code-name
    if update_stubs {
        println!("Updating structure files with code-names...");
        sync_structure_files_with_atoms(&probe_index, &probe_atoms, &config.structure_root)?;
    }

    println!("Done.");
    Ok(())
}

/// Run probe-verus stubify to generate stubs.json from .md files.
fn generate_stubs(structure_root: &Path, stubs_path: &Path) -> Result<HashMap<String, Value>> {
    check_probe_verus_or_exit()?;

    if let Some(parent) = stubs_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!(
        "Running probe-verus stubify on {}...",
        structure_root.display()
    );

    let output = run_command(
        "probe-verus",
        &[
            "stubify",
            structure_root.to_str().unwrap(),
            "-o",
            stubs_path.to_str().unwrap(),
        ],
        None,
    )?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error: probe-verus stubify failed.");
        if !stderr.is_empty() {
            eprintln!("{}", stderr);
        }
        bail!("probe-verus stubify failed");
    }

    println!("Stubs saved to {}", stubs_path.display());

    let content = std::fs::read_to_string(stubs_path)?;
    let stubs: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(stubs)
}

/// Run probe-verus atomize on the project and save results to atoms.json.
fn generate_probe_atoms(
    project_root: &Path,
    atoms_path: &Path,
) -> Result<HashMap<String, Value>> {
    check_probe_verus_or_exit()?;

    if let Some(parent) = atoms_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!(
        "Running probe-verus atomize on {}...",
        project_root.display()
    );

    let output = run_command(
        "probe-verus",
        &[
            "atomize",
            project_root.to_str().unwrap(),
            "-o",
            atoms_path.to_str().unwrap(),
            "-r",
        ],
        None,
    )?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error: probe-verus atomize failed.");
        if !stderr.is_empty() {
            eprintln!("{}", stderr);
        }
        bail!("probe-verus atomize failed");
    }

    // Clean up generated intermediate files
    for cleanup_file in ["data/index.scip", "data/index.scip.json"] {
        let cleanup_path = project_root.join(cleanup_file);
        if cleanup_path.exists() {
            let _ = std::fs::remove_file(&cleanup_path);
        }
    }

    let data_dir = project_root.join("data");
    if data_dir.exists() && data_dir.is_dir() {
        if std::fs::read_dir(&data_dir)?.next().is_none() {
            let _ = std::fs::remove_dir(&data_dir);
        }
    }

    println!("Atoms saved to {}", atoms_path.display());

    let content = std::fs::read_to_string(atoms_path)?;
    let atoms: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(atoms)
}

/// Filter probe atoms to only those where probe-name starts with prefix.
fn filter_probe_atoms(
    probe_atoms: &HashMap<String, Value>,
    prefix: &str,
) -> HashMap<String, Value> {
    let uri_prefix = format!("probe:{}/", prefix);
    probe_atoms
        .iter()
        .filter(|(k, _)| k.starts_with(&uri_prefix))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Build an interval tree index for fast line-based lookups.
fn generate_probe_index(
    probe_atoms: &HashMap<String, Value>,
) -> HashMap<String, IntervalTree<u32, String>> {
    let mut trees: HashMap<String, Vec<(std::ops::Range<u32>, String)>> = HashMap::new();

    for (probe_name, atom_data) in probe_atoms {
        let code_path = match atom_data.get("code-path").and_then(|v| v.as_str()) {
            Some(p) => p.to_string(),
            None => continue,
        };

        let code_text = match atom_data.get("code-text") {
            Some(ct) => ct,
            None => continue,
        };

        let lines_start = match code_text.get("lines-start").and_then(|v| v.as_u64()) {
            Some(l) => l as u32,
            None => continue,
        };

        let lines_end = match code_text.get("lines-end").and_then(|v| v.as_u64()) {
            Some(l) => l as u32,
            None => continue,
        };

        trees
            .entry(code_path)
            .or_default()
            .push((lines_start..lines_end + 1, probe_name.clone()));
    }

    trees
        .into_iter()
        .map(|(k, v)| (k, v.into_iter().collect()))
        .collect()
}

/// Look up code-name from code-path and code-line using the probe index.
fn lookup_code_name(
    code_path: &str,
    code_line: u32,
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
) -> Option<String> {
    let tree = probe_index.get(code_path)?;

    let matching_intervals: Vec<_> = tree
        .query(code_line..code_line + 1)
        .filter(|iv| iv.range.start == code_line)
        .collect();

    if matching_intervals.is_empty() {
        return None;
    }

    Some(matching_intervals[0].value.clone())
}

/// Enrich stubs with code-name and all metadata from atoms.
fn enrich_stubs_with_atoms(
    stubs: HashMap<String, Value>,
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
    probe_atoms: &HashMap<String, Value>,
) -> Result<HashMap<String, Value>> {
    let mut result = HashMap::new();
    let mut enriched_count = 0;
    let mut skipped_count = 0;

    for (file_path, entry) in stubs {
        // Get code-path and code-line from the stub entry
        let code_path = entry.get("code-path").and_then(|v| v.as_str());
        let code_line = entry.get("code-line").and_then(|v| v.as_u64()).map(|l| l as u32);

        let (code_path, code_line) = match (code_path, code_line) {
            (Some(p), Some(l)) => (p, l),
            _ => {
                eprintln!(
                    "WARNING: Missing code-path or code-line for {}",
                    file_path
                );
                skipped_count += 1;
                result.insert(file_path, entry);
                continue;
            }
        };

        // Look up code-name from code-path and code-line
        let code_name = match lookup_code_name(code_path, code_line, probe_index) {
            Some(name) => name,
            None => {
                eprintln!(
                    "WARNING: No atom found for {}:{} ({})",
                    code_path, code_line, file_path
                );
                skipped_count += 1;
                result.insert(file_path, entry);
                continue;
            }
        };

        // Get all metadata from atoms.json for this code-name
        let atom = match probe_atoms.get(&code_name) {
            Some(a) => a,
            None => {
                eprintln!(
                    "WARNING: Atom not found for code-name {} ({})",
                    code_name, file_path
                );
                skipped_count += 1;
                result.insert(file_path, entry);
                continue;
            }
        };

        // Build enriched entry with all atom metadata
        let enriched_entry = build_enriched_entry(&code_name, atom);
        result.insert(file_path, enriched_entry);
        enriched_count += 1;
    }

    println!("Entries enriched: {}", enriched_count);
    println!("Skipped: {}", skipped_count);

    Ok(result)
}

/// Build an enriched entry from atom data.
fn build_enriched_entry(code_name: &str, atom: &Value) -> Value {
    let code_path = atom
        .get("code-path")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let code_text = atom.get("code-text");

    let lines_start = code_text
        .and_then(|ct| ct.get("lines-start"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);

    let lines_end = code_text
        .and_then(|ct| ct.get("lines-end"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);

    let code_module = atom
        .get("code-module")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let dependencies = atom
        .get("dependencies")
        .cloned()
        .unwrap_or_else(|| json!([]));

    let display_name = atom
        .get("display-name")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    json!({
        "code-path": code_path,
        "code-lines": {
            "start": lines_start,
            "end": lines_end,
        },
        "code-name": code_name,
        "code-module": code_module,
        "dependencies": dependencies,
        "display-name": display_name,
    })
}

/// Sync structure .md files with probe atoms index (update code-name field).
fn sync_structure_files_with_atoms(
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
    probe_atoms: &HashMap<String, Value>,
    structure_root: &Path,
) -> Result<()> {
    let mut updated_count = 0;
    let mut not_found_count = 0;

    for entry in walkdir::WalkDir::new(structure_root)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let path = entry.path();
        if !path.extension().map_or(false, |ext| ext == "md") {
            continue;
        }

        let frontmatter = match parse_frontmatter(path) {
            Ok(fm) => fm,
            Err(_) => continue,
        };

        let code_path = frontmatter
            .get("code-path")
            .and_then(|v| v.as_str());
        let code_line = frontmatter
            .get("code-line")
            .and_then(|v| v.as_u64())
            .map(|l| l as u32);

        let (code_path, code_line) = match (code_path, code_line) {
            (Some(p), Some(l)) => (p, l),
            _ => {
                not_found_count += 1;
                continue;
            }
        };

        // Look up code-name
        let code_name = match lookup_code_name(code_path, code_line, probe_index) {
            Some(name) => name,
            None => {
                eprintln!(
                    "WARNING: No atom found for {}:{} ({})",
                    code_path, code_line, path.display()
                );
                not_found_count += 1;
                continue;
            }
        };

        // Verify code-name exists in atoms
        if !probe_atoms.contains_key(&code_name) {
            eprintln!(
                "WARNING: code-name {} not in atoms ({})",
                code_name, path.display()
            );
            not_found_count += 1;
            continue;
        }

        // Read original file content to preserve body
        let original_content = std::fs::read_to_string(path)?;
        let body_start = original_content
            .find("\n---\n")
            .map(|pos| pos + 5)
            .and_then(|start| {
                original_content[start..]
                    .find("\n---\n")
                    .map(|p| start + p + 5)
            });

        let body = body_start.map(|start| original_content[start..].to_string());

        // Build updated frontmatter
        let mut metadata: HashMap<String, Value> = frontmatter
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        metadata.insert("code-name".to_string(), json!(code_name));

        crate::utils::write_frontmatter_file(path, &metadata, body.as_deref())?;
        updated_count += 1;
    }

    println!("Structure files updated: {}", updated_count);
    println!("Not found/skipped: {}", not_found_count);

    Ok(())
}
