//! Specify subcommand implementation.
//!
//! Check specification status and manage spec certs.

use crate::certs::{create_cert, get_existing_certs};
use crate::config::ConfigPaths;
use crate::probe;
use crate::utils::{display_menu, run_command};
use std::collections::HashSet;
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Run the specify subcommand.
pub fn run(project_root: PathBuf) -> Result<()> {
    let project_root = project_root
        .canonicalize()
        .context("Failed to resolve project root")?;
    let config = ConfigPaths::load(&project_root)?;

    let specs_path = config.verilib_path.join("specs.json");
    let specs_data = run_probe_specify(&project_root, &specs_path, &config.atoms_path)?;

    let existing_certs = get_existing_certs(&config.certs_specify_dir)?;
    println!("Found {} existing certs", existing_certs.len());

    let uncertified = find_uncertified_functions(
        &specs_data,
        &config.structure_json_path,
        &existing_certs,
    )?;

    let newly_certified = collect_certifications(&uncertified, &config.certs_specify_dir)?;

    let all_certified: HashSet<String> = existing_certs
        .union(&newly_certified)
        .cloned()
        .collect();

    update_stubs_specification_status(&config.structure_json_path, &all_certified)?;

    println!("Done.");
    Ok(())
}

/// Find functions with specs that are in structure but not yet certified.
fn find_uncertified_functions(
    specs_data: &HashMap<String, Value>,
    stubs_path: &Path,
    existing_certs: &HashSet<String>,
) -> Result<HashMap<String, Value>> {
    let functions_with_specs = filter_functions_with_specs(specs_data);
    println!(
        "\nFound {} functions with specs in codebase",
        functions_with_specs.len()
    );

    let structure_names = get_structure_names(stubs_path)?;
    println!("Found {} functions in structure", structure_names.len());

    let functions_in_structure: HashMap<String, Value> = functions_with_specs
        .into_iter()
        .filter(|(name, _)| structure_names.contains(name))
        .collect();
    println!(
        "Found {} functions with specs in structure",
        functions_in_structure.len()
    );

    let uncertified: HashMap<String, Value> = functions_in_structure
        .into_iter()
        .filter(|(name, _)| !existing_certs.contains(name))
        .collect();

    Ok(uncertified)
}

/// Display menu for uncertified functions and create certs for selected ones.
/// Returns the set of newly certified function names.
fn collect_certifications(
    uncertified: &HashMap<String, Value>,
    certs_dir: &Path,
) -> Result<HashSet<String>> {
    let mut newly_certified = HashSet::new();

    if uncertified.is_empty() {
        println!("\nAll functions with specs in structure are already validated!");
        return Ok(newly_certified);
    }

    println!(
        "\n{} functions with specs need certification",
        uncertified.len()
    );

    let mut uncertified_list: Vec<(String, Value)> = uncertified
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    uncertified_list.sort_by(|a, b| a.0.cmp(&b.0));

    let selected_indices = display_menu(&uncertified_list, |i, _name, info| {
        let func_name = info.get("name").and_then(|v| v.as_str()).unwrap_or("?");
        let file_path = info.get("file").and_then(|v| v.as_str()).unwrap_or("?");
        let start_line = info
            .get("start_line")
            .and_then(|v| v.as_u64())
            .map(|l| l.to_string())
            .unwrap_or_else(|| "?".to_string());

        format!("  [{}] {} ({}:{})", i, func_name, file_path, start_line)
    })?;

    if selected_indices.is_empty() {
        println!("\nNo functions selected.");
        return Ok(newly_certified);
    }

    println!(
        "\nCreating certs for {} functions...",
        selected_indices.len()
    );

    for idx in &selected_indices {
        let (name, _) = &uncertified_list[*idx];
        newly_certified.insert(name.clone());
        let cert_path = create_cert(certs_dir, name)?;
        println!(
            "  Created: {}",
            cert_path.file_name().unwrap_or_default().to_string_lossy()
        );
    }

    println!(
        "\nCreated {} cert files in {}",
        selected_indices.len(),
        certs_dir.display()
    );

    Ok(newly_certified)
}

/// Run probe-verus specify and return the results.
fn run_probe_specify(
    project_root: &Path,
    specs_path: &Path,
    atoms_path: &Path,
) -> Result<HashMap<String, Value>> {
    probe::require_installed()?;

    if let Some(parent) = specs_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!(
        "Running probe-verus specify on {}...",
        project_root.display()
    );

    let output = run_command(
        "probe-verus",
        &[
            "specify",
            project_root.to_str().unwrap(),
            "-o",
            specs_path.to_str().unwrap(),
            "-a",
            atoms_path.to_str().unwrap(),
        ],
        Some(project_root),
    )?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error: probe-verus specify failed.");
        if !stderr.is_empty() {
            eprintln!("{}", stderr);
        }
        bail!("probe-verus specify failed");
    }

    println!("Specs saved to {}", specs_path.display());

    let content = std::fs::read_to_string(specs_path)?;
    let specs: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(specs)
}

/// Filter specs data to only functions that are specified.
fn filter_functions_with_specs(specs_data: &HashMap<String, Value>) -> HashMap<String, Value> {
    specs_data
        .iter()
        .filter(|(_, func_info)| {
            func_info
                .get("specified")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Get structure names from stubs.json file.
fn get_structure_names(stubs_path: &Path) -> Result<HashSet<String>> {
    if !stubs_path.exists() {
        eprintln!("Warning: {} not found", stubs_path.display());
        return Ok(HashSet::new());
    }

    let content = std::fs::read_to_string(stubs_path)?;
    let stubs: HashMap<String, Value> = serde_json::from_str(&content)?;

    let names = stubs
        .values()
        .filter_map(|entry| entry.get("code-name").and_then(|v| v.as_str()))
        .map(|s| s.to_string())
        .collect();

    Ok(names)
}

/// Update stubs.json with specification statuses.
fn update_stubs_specification_status(
    stubs_path: &Path,
    certified_names: &HashSet<String>,
) -> Result<()> {
    if !stubs_path.exists() {
        return Ok(());
    }

    let content = std::fs::read_to_string(stubs_path)?;
    let mut stubs: HashMap<String, Value> = serde_json::from_str(&content)?;

    for entry in stubs.values_mut() {
        if let Some(obj) = entry.as_object_mut() {
            let specified = obj
                .get("code-name")
                .and_then(|v| v.as_str())
                .map(|name| certified_names.contains(name))
                .unwrap_or(false);

            obj.insert("specified".to_string(), Value::Bool(specified));
        }
    }

    let updated_content = serde_json::to_string_pretty(&stubs)?;
    std::fs::write(stubs_path, updated_content)?;

    println!("Updated specification status in {}", stubs_path.display());
    Ok(())
}
