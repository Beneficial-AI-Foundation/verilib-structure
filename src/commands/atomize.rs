//! Atomize subcommand implementation.
//!
//! Enrich structure files with metadata from SCIP atoms or blueprint.

use crate::config::constants::PROBE_PREFIX;
use crate::config::ConfigPaths;
use crate::utils::{check_probe_verus_or_exit, parse_frontmatter, run_command};
use crate::{StructureForm, StructureType};
use anyhow::{bail, Context, Result};
use intervaltree::IntervalTree;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Run the atomize subcommand.
pub fn run(project_root: PathBuf, update_stubs: bool) -> Result<()> {
    let project_root = project_root.canonicalize()
        .context("Failed to resolve project root")?;
    let config = ConfigPaths::load(&project_root)?;

    let structure_type = config.config.get_structure_type()?;
    let structure_form = config.config.get_structure_form()?;

    match structure_type {
        StructureType::Blueprint => {
            run_blueprint_atomize(&config, structure_form)?;
        }
        StructureType::DalekLite => {
            run_dalek_atomize(&project_root, &config, structure_form, update_stubs)?;
        }
    }

    Ok(())
}

// =============================================================================
// Blueprint atomize
// =============================================================================

fn run_blueprint_atomize(config: &ConfigPaths, structure_form: StructureForm) -> Result<()> {
    if !config.blueprint_json_path.exists() {
        bail!(
            "{} not found. Run 'verilib-structure create' first.",
            config.blueprint_json_path.display()
        );
    }

    println!("Loading blueprint from {}...", config.blueprint_json_path.display());
    let content = std::fs::read_to_string(&config.blueprint_json_path)?;
    let blueprint_data: HashMap<String, Value> = serde_json::from_str(&content)?;

    match structure_form {
        StructureForm::Json => {
            if !config.structure_json_path.exists() {
                bail!("{} not found", config.structure_json_path.display());
            }

            println!("Loading structure from {}...", config.structure_json_path.display());
            let content = std::fs::read_to_string(&config.structure_json_path)?;
            let structure: HashMap<String, Value> = serde_json::from_str(&content)?;

            println!("Populating structure metadata from blueprint...");
            let metadata = populate_blueprint_json_metadata(&structure, &blueprint_data)?;

            println!("Saving metadata to {}...", config.structure_meta_path.display());
            let content = serde_json::to_string_pretty(&metadata)?;
            std::fs::write(&config.structure_meta_path, content)?;
            println!("Done.");
        }
        StructureForm::Files => {
            println!(
                "Populating blueprint metadata files in {}...",
                config.structure_root.display()
            );
            populate_blueprint_files_metadata(&blueprint_data, &config.structure_root)?;
            println!("Done.");
        }
    }

    Ok(())
}

/// Generate metadata dictionary from blueprint structure JSON.
fn populate_blueprint_json_metadata(
    structure: &HashMap<String, Value>,
    blueprint_data: &HashMap<String, Value>,
) -> Result<HashMap<String, Value>> {
    let mut result = HashMap::new();
    let mut created_count = 0;
    let mut skipped_count = 0;

    for (file_path, entry) in structure {
        let veri_name = match entry.get("veri-name").and_then(|v| v.as_str()) {
            Some(name) => name,
            None => {
                eprintln!("WARNING: Missing veri-name for {}", file_path);
                skipped_count += 1;
                continue;
            }
        };

        let node_id = veri_name.strip_prefix("veri:").unwrap_or(veri_name);

        if !blueprint_data.contains_key(node_id) {
            eprintln!(
                "WARNING: Node '{}' not found in blueprint.json for {}",
                node_id, file_path
            );
            skipped_count += 1;
            continue;
        }

        let node_info = &blueprint_data[node_id];

        let mut all_deps = Vec::new();
        if let Some(type_deps) = node_info.get("type-dependencies").and_then(|v| v.as_array()) {
            for dep in type_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }
        if let Some(term_deps) = node_info.get("term-dependencies").and_then(|v| v.as_array()) {
            for dep in term_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }

        result.insert(
            veri_name.to_string(),
            json!({
                "dependencies": all_deps,
                "visible": true,
            }),
        );
        created_count += 1;
    }

    println!("Metadata entries created: {}", created_count);
    println!("Skipped: {}", skipped_count);

    Ok(result)
}

/// Generate metadata files for each blueprint structure .md file.
fn populate_blueprint_files_metadata(
    blueprint_data: &HashMap<String, Value>,
    structure_root: &Path,
) -> Result<()> {
    let mut created_count = 0;
    let mut skipped_count = 0;

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
            Err(_) => {
                skipped_count += 1;
                continue;
            }
        };

        let veri_name = match frontmatter.get("veri-name").and_then(|v| v.as_str()) {
            Some(name) => name,
            None => {
                eprintln!("WARNING: Missing veri-name for {}", path.display());
                skipped_count += 1;
                continue;
            }
        };

        let node_id = veri_name.strip_prefix("veri:").unwrap_or(veri_name);

        if !blueprint_data.contains_key(node_id) {
            eprintln!(
                "WARNING: Node '{}' not found in blueprint.json for {}",
                node_id,
                path.display()
            );
            skipped_count += 1;
            continue;
        }

        let node_info = &blueprint_data[node_id];

        let mut all_deps = Vec::new();
        if let Some(type_deps) = node_info.get("type-dependencies").and_then(|v| v.as_array()) {
            for dep in type_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }
        if let Some(term_deps) = node_info.get("term-dependencies").and_then(|v| v.as_array()) {
            for dep in term_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }

        let meta_data = json!({
            "veri-name": veri_name,
            "dependencies": all_deps,
            "visible": true,
        });

        let meta_file = path.with_extension("meta.verilib");
        let content = serde_json::to_string_pretty(&meta_data)?;
        std::fs::write(&meta_file, content)?;

        // Write atom file with content
        let atom_content = node_info
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let atom_file = path.with_extension("atom.verilib");
        std::fs::write(&atom_file, atom_content)?;

        created_count += 1;
    }

    println!("Metadata files created: {}", created_count);
    println!("Skipped: {}", skipped_count);

    Ok(())
}

// =============================================================================
// Dalek-lite atomize
// =============================================================================

fn run_dalek_atomize(
    project_root: &Path,
    config: &ConfigPaths,
    structure_form: StructureForm,
    update_stubs: bool,
) -> Result<()> {
    let probe_atoms = generate_probe_atoms(project_root, &config.atoms_path)?;
    let probe_atoms = filter_probe_atoms(&probe_atoms, PROBE_PREFIX);
    let probe_index = generate_probe_index(&probe_atoms);

    match structure_form {
        StructureForm::Json => {
            if !config.structure_json_path.exists() {
                bail!("{} not found", config.structure_json_path.display());
            }

            println!("Loading structure from {}...", config.structure_json_path.display());
            let content = std::fs::read_to_string(&config.structure_json_path)?;
            let structure: HashMap<String, Value> = serde_json::from_str(&content)?;

            // Sync to get code-names (in memory)
            println!("Syncing structure with probe atoms...");
            let structure = sync_structure_json_with_atoms(structure, &probe_index, &probe_atoms)?;

            println!("Enriching structure with atom metadata...");
            let enriched = enrich_structure_json(&structure, &probe_atoms)?;

            println!("Saving enriched structure to {}...", config.structure_json_path.display());
            let content = serde_json::to_string_pretty(&enriched)?;
            std::fs::write(&config.structure_json_path, content)?;
            println!("Done.");
        }
        StructureForm::Files => {
            if update_stubs {
                println!(
                    "Syncing structure files in {} with probe atoms...",
                    config.structure_root.display()
                );
                sync_structure_files_with_atoms(&probe_index, &probe_atoms, &config.structure_root)?;
            }

            println!("Populating structure metadata files...");
            populate_structure_files_metadata(&probe_atoms, &probe_index, &config.structure_root, project_root)?;
            println!("Done.");
        }
    }

    Ok(())
}

/// Run probe-verus atomize on the project and save results to atoms.json.
fn generate_probe_atoms(project_root: &Path, atoms_path: &Path) -> Result<HashMap<String, Value>> {
    check_probe_verus_or_exit()?;

    if let Some(parent) = atoms_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!("Running probe-verus atomize on {}...", project_root.display());

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

    println!("Results saved to {}", atoms_path.display());

    let content = std::fs::read_to_string(atoms_path)?;
    let atoms: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(atoms)
}

/// Filter probe atoms to only those where probe-name starts with prefix.
fn filter_probe_atoms(probe_atoms: &HashMap<String, Value>, prefix: &str) -> HashMap<String, Value> {
    let uri_prefix = format!("probe:{}/", prefix);
    probe_atoms
        .iter()
        .filter(|(k, _)| k.starts_with(&uri_prefix))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Build an interval tree index for fast line-based lookups.
fn generate_probe_index(probe_atoms: &HashMap<String, Value>) -> HashMap<String, IntervalTree<u32, String>> {
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

/// Update a structure entry with probe atom data.
fn update_entry_from_atoms(
    entry: &Value,
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
    probe_atoms: &HashMap<String, Value>,
    context: &str,
) -> Result<(Value, Option<String>)> {
    let code_path = entry.get("code-path").and_then(|v| v.as_str());
    let line_start = entry.get("code-line").and_then(|v| v.as_u64()).map(|l| l as u32);
    let existing_probe_name = entry.get("code-name").and_then(|v| v.as_str());

    let mut updated = entry.clone();

    if let Some(probe_name) = existing_probe_name {
        if let Some(atom) = probe_atoms.get(probe_name) {
            let atom_code_path = atom.get("code-path").and_then(|v| v.as_str());
            let atom_code_text = atom.get("code-text");
            let atom_line_start = atom_code_text
                .and_then(|ct| ct.get("lines-start"))
                .and_then(|v| v.as_u64())
                .map(|l| l as u32);

            if code_path != atom_code_path {
                eprintln!(
                    "WARNING: code-path mismatch for {}: '{}' will be overwritten with '{}'",
                    context,
                    code_path.unwrap_or(""),
                    atom_code_path.unwrap_or("")
                );
            }

            if line_start != atom_line_start {
                eprintln!(
                    "WARNING: code-line mismatch for {}: {:?} will be overwritten with {:?}",
                    context, line_start, atom_line_start
                );
            }

            if let Some(obj) = updated.as_object_mut() {
                if let Some(p) = atom_code_path {
                    obj.insert("code-path".to_string(), json!(p));
                }
                if let Some(l) = atom_line_start {
                    obj.insert("code-line".to_string(), json!(l));
                }
            }

            return Ok((updated, None));
        } else {
            eprintln!(
                "WARNING: code-name '{}' not found in probe_atoms for {}, looking up by code-path/code-line",
                probe_name, context
            );
        }
    }

    let (code_path, line_start) = match (code_path, line_start) {
        (Some(p), Some(l)) => (p, l),
        _ => {
            eprintln!(
                "WARNING: Missing code-path or code-line for {}; code-name will not be generated",
                context
            );
            return Ok((updated, None));
        }
    };

    let tree = match probe_index.get(code_path) {
        Some(t) => t,
        None => {
            return Ok((
                updated,
                Some(format!("code-path '{}' not found in probe_index", code_path)),
            ));
        }
    };

    let matching_intervals: Vec<_> = tree
        .query(line_start..line_start + 1)
        .filter(|iv| iv.range.start == line_start)
        .collect();

    if matching_intervals.is_empty() {
        return Ok((
            updated,
            Some(format!(
                "No interval starting at line {} in {}",
                line_start, code_path
            )),
        ));
    }

    if matching_intervals.len() > 1 {
        eprintln!(
            "WARNING: Multiple intervals starting at line {} in {} for {}",
            line_start, code_path, context
        );
    }

    let probe_name = &matching_intervals[0].value;
    if let Some(obj) = updated.as_object_mut() {
        obj.insert("code-name".to_string(), json!(probe_name));
    }

    Ok((updated, None))
}

/// Sync structure dictionary with probe atoms index.
fn sync_structure_json_with_atoms(
    structure: HashMap<String, Value>,
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
    probe_atoms: &HashMap<String, Value>,
) -> Result<HashMap<String, Value>> {
    let mut updated_count = 0;
    let mut not_found_count = 0;
    let mut result = HashMap::new();

    for (file_path, entry) in structure {
        let (updated, error) = update_entry_from_atoms(&entry, probe_index, probe_atoms, &file_path)?;

        if let Some(err) = error {
            eprintln!("WARNING: {} for {}", err, file_path);
            not_found_count += 1;
            result.insert(file_path, entry);
        } else {
            result.insert(file_path, updated);
            updated_count += 1;
        }
    }

    println!("Structure entries updated: {}", updated_count);
    println!("Not found/skipped: {}", not_found_count);

    Ok(result)
}

/// Sync structure .md files with probe atoms index.
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

        let entry_value = json!(frontmatter);
        let (updated, error) =
            update_entry_from_atoms(&entry_value, probe_index, probe_atoms, &path.display().to_string())?;

        if let Some(err) = error {
            eprintln!("WARNING: {} for {}", err, path.display());
            not_found_count += 1;
            continue;
        }

        // Read original file content to preserve body
        let original_content = std::fs::read_to_string(path)?;
        let body_start = original_content
            .find("\n---\n")
            .map(|pos| pos + 5)
            .and_then(|start| original_content[start..].find("\n---\n").map(|p| start + p + 5));

        let body = body_start.map(|start| original_content[start..].to_string());

        // Write updated frontmatter
        let metadata: HashMap<String, Value> = updated
            .as_object()
            .map(|obj| obj.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();

        crate::utils::write_frontmatter_file(path, &metadata, body.as_deref())?;
        updated_count += 1;
    }

    println!("Structure files updated: {}", updated_count);
    println!("Not found/skipped: {}", not_found_count);

    Ok(())
}

/// Generate enriched entry from probe atom data.
/// Returns a JSON object with code-path, code-lines, code-name, code-module, dependencies, display-name.
fn generate_enriched_entry(
    probe_name: &str,
    probe_atoms: &HashMap<String, Value>,
) -> Result<Option<Value>> {
    let atom = match probe_atoms.get(probe_name) {
        Some(a) => a,
        None => return Ok(None),
    };

    let code_path = match atom.get("code-path").and_then(|v| v.as_str()) {
        Some(p) => p,
        None => return Ok(None),
    };

    let code_text = match atom.get("code-text") {
        Some(ct) => ct,
        None => return Ok(None),
    };

    let lines_start = match code_text.get("lines-start").and_then(|v| v.as_u64()) {
        Some(l) => l,
        None => return Ok(None),
    };

    let lines_end = match code_text.get("lines-end").and_then(|v| v.as_u64()) {
        Some(l) => l,
        None => return Ok(None),
    };

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

    Ok(Some(json!({
        "code-path": code_path,
        "code-lines": {
            "start": lines_start,
            "end": lines_end,
        },
        "code-name": probe_name,
        "code-module": code_module,
        "dependencies": dependencies,
        "display-name": display_name,
    })))
}

/// Enrich structure JSON with atom metadata.
/// Keys are file paths, values are enriched entries with code-path, code-lines, code-name, code-module, dependencies, display-name.
fn enrich_structure_json(
    structure: &HashMap<String, Value>,
    probe_atoms: &HashMap<String, Value>,
) -> Result<HashMap<String, Value>> {
    let mut result = HashMap::new();
    let mut enriched_count = 0;
    let mut skipped_count = 0;

    for (file_path, entry) in structure {
        let probe_name = match entry.get("code-name").and_then(|v| v.as_str()) {
            Some(name) => name,
            None => {
                eprintln!("WARNING: Missing or invalid code-name for {}", file_path);
                skipped_count += 1;
                // Keep original entry if no code-name
                result.insert(file_path.clone(), entry.clone());
                continue;
            }
        };

        match generate_enriched_entry(probe_name, probe_atoms)? {
            Some(enriched_entry) => {
                result.insert(file_path.clone(), enriched_entry);
                enriched_count += 1;
            }
            None => {
                eprintln!("WARNING: Missing atom data for {} ({})", file_path, probe_name);
                skipped_count += 1;
                // Keep original entry if enrichment fails
                result.insert(file_path.clone(), entry.clone());
            }
        }
    }

    println!("Entries enriched: {}", enriched_count);
    println!("Skipped: {}", skipped_count);

    Ok(result)
}

/// Generate metadata files for each structure .md file.
fn populate_structure_files_metadata(
    probe_atoms: &HashMap<String, Value>,
    probe_index: &HashMap<String, IntervalTree<u32, String>>,
    structure_root: &Path,
    project_root: &Path,
) -> Result<()> {
    let mut created_count = 0;
    let mut skipped_count = 0;

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
            Err(_) => {
                skipped_count += 1;
                continue;
            }
        };

        // Try to get code-name from frontmatter, or look it up from probe_index
        let probe_name: String = match frontmatter.get("code-name").and_then(|v| v.as_str()) {
            Some(name) => name.to_string(),
            None => {
                // Look up code-name from probe_index using code-path and code-line
                let code_path = frontmatter.get("code-path").and_then(|v| v.as_str());
                let line_start = frontmatter.get("code-line").and_then(|v| v.as_u64()).map(|l| l as u32);

                match (code_path, line_start) {
                    (Some(cp), Some(ls)) => {
                        if let Some(tree) = probe_index.get(cp) {
                            let matching: Vec<_> = tree
                                .query(ls..ls + 1)
                                .filter(|iv| iv.range.start == ls)
                                .collect();
                            if !matching.is_empty() {
                                matching[0].value.clone()
                            } else {
                                eprintln!("WARNING: No atom found at {}:{} for {}", cp, ls, path.display());
                                skipped_count += 1;
                                continue;
                            }
                        } else {
                            eprintln!("WARNING: code-path '{}' not in probe_index for {}", cp, path.display());
                            skipped_count += 1;
                            continue;
                        }
                    }
                    _ => {
                        eprintln!("WARNING: Missing code-name and code-path/code-line for {}", path.display());
                        skipped_count += 1;
                        continue;
                    }
                }
            }
        };

        let meta_data = match generate_enriched_entry(&probe_name, probe_atoms)? {
            Some(md) => md,
            None => {
                eprintln!("WARNING: Missing code-path or line info for {}", path.display());
                skipped_count += 1;
                continue;
            }
        };

        // Write metadata file
        let meta_file = path.with_extension("meta.verilib");
        let content = serde_json::to_string_pretty(&meta_data)?;
        std::fs::write(&meta_file, content)?;

        // Extract and write code content
        let code_path = meta_data
            .get("code-path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let lines_start = meta_data
            .get("code-lines")
            .and_then(|cl| cl.get("start"))
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as usize;
        let lines_end = meta_data
            .get("code-lines")
            .and_then(|cl| cl.get("end"))
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as usize;

        let source_file = project_root.join(code_path);
        if source_file.exists() {
            let source_content = std::fs::read_to_string(&source_file)?;
            let lines: Vec<&str> = source_content.lines().collect();

            if lines_start > 0 && lines_end <= lines.len() {
                let extracted: Vec<&str> = lines[lines_start - 1..lines_end].to_vec();
                let atom_content = extracted.join("\n");

                let atom_file = path.with_extension("atom.verilib");
                std::fs::write(&atom_file, atom_content)?;
            }
        } else {
            eprintln!("WARNING: Source file not found: {}", source_file.display());
        }

        created_count += 1;
    }

    println!("Metadata files created: {}", created_count);
    println!("Skipped: {}", skipped_count);

    Ok(())
}
