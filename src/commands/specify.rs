//! Specify subcommand implementation.
//!
//! Check specification status and manage spec certs.

use crate::config::constants::BLUEPRINT_SPEC_STATUSES;
use crate::config::ConfigPaths;
use crate::utils::{
    check_probe_verus_or_exit, create_cert, display_menu, get_existing_certs, get_structure_names,
    run_command,
};
use crate::StructureType;
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

    let structure_type = config.config.get_structure_type()?;

    let functions_in_structure = match structure_type {
        StructureType::Blueprint => {
            let functions_with_specs = get_blueprint_functions_with_specs(&config.blueprint_json_path)?;
            println!(
                "\nFound {} functions with specs in blueprint",
                functions_with_specs.len()
            );

            let structure_names = get_structure_names(
                structure_type,
                &config.structure_root,
            )?;
            println!("Found {} functions in structure", structure_names.len());

            let functions_in_structure: HashMap<String, Value> = functions_with_specs
                .into_iter()
                .filter(|(name, _)| structure_names.contains(name))
                .collect();
            println!(
                "Found {} functions with specs in structure",
                functions_in_structure.len()
            );

            functions_in_structure
        }

        StructureType::DalekLite => {
            let specs_path = config.verilib_path.join("specs.json");
            let specs_data = run_probe_specify(&project_root, &specs_path, &config.atoms_path)?;

            let functions_with_specs = get_functions_with_specs(&specs_data);
            println!(
                "\nFound {} functions with specs in codebase",
                functions_with_specs.len()
            );

            let structure_names = get_structure_names(
                structure_type,
                &config.structure_root,
            )?;
            println!("Found {} functions in structure", structure_names.len());

            let functions_in_structure: HashMap<String, Value> = functions_with_specs
                .into_iter()
                .filter(|(name, _)| structure_names.contains(name))
                .collect();
            println!(
                "Found {} functions with specs in structure",
                functions_in_structure.len()
            );

            functions_in_structure
        }
    };

    let existing_certs = get_existing_certs(&config.certs_specify_dir)?;
    println!("Found {} existing certs", existing_certs.len());

    let uncertified: HashMap<String, Value> = functions_in_structure
        .into_iter()
        .filter(|(name, _)| !existing_certs.contains(name))
        .collect();

    if uncertified.is_empty() {
        println!("\nAll functions with specs in structure are already validated!");
        return Ok(());
    }

    println!("\n{} functions with specs need certification", uncertified.len());

    let mut uncertified_list: Vec<(String, Value)> = uncertified.into_iter().collect();
    uncertified_list.sort_by(|a, b| a.0.cmp(&b.0));

    let selected_indices = display_menu(&uncertified_list, structure_type, |i, name, info| {
        match structure_type {
            StructureType::Blueprint => {
                let kind = info.get("kind").and_then(|v| v.as_str()).unwrap_or("?");
                let type_status = info
                    .get("type-status")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?");
                let node_id = name.strip_prefix("veri:").unwrap_or(name);
                format!(
                    "  [{}] {}\n      Kind: {}, Status: {}",
                    i, node_id, kind, type_status
                )
            }
            StructureType::DalekLite => {
                let has_requires = info
                    .get("has_requires")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let has_ensures = info
                    .get("has_ensures")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);

                let mut spec_types = Vec::new();
                if has_requires {
                    spec_types.push("requires");
                }
                if has_ensures {
                    spec_types.push("ensures");
                }
                let spec_str = spec_types.join(", ");

                let func_name = info.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                let file_path = info.get("file").and_then(|v| v.as_str()).unwrap_or("?");
                let start_line = info
                    .get("start_line")
                    .and_then(|v| v.as_u64())
                    .map(|l| l.to_string())
                    .unwrap_or_else(|| "?".to_string());

                format!(
                    "  [{}] {} ({}:{})\n      Specs: {}",
                    i, func_name, file_path, start_line, spec_str
                )
            }
        }
    })?;

    if selected_indices.is_empty() {
        println!("\nNo functions selected.");
        return Ok(());
    }

    println!(
        "\nCreating certs for {} functions...",
        selected_indices.len()
    );

    for idx in &selected_indices {
        let (name, _) = &uncertified_list[*idx];
        let cert_path = create_cert(&config.certs_specify_dir, name)?;
        println!(
            "  Created: {}",
            cert_path.file_name().unwrap_or_default().to_string_lossy()
        );
    }

    println!(
        "\nDone. Created {} cert files in {}",
        selected_indices.len(),
        config.certs_specify_dir.display()
    );

    Ok(())
}

/// Run probe-verus specify and return the results.
fn run_probe_specify(
    project_root: &Path,
    specs_path: &Path,
    atoms_path: &Path,
) -> Result<HashMap<String, Value>> {
    check_probe_verus_or_exit()?;

    if let Some(parent) = specs_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!("Running probe-verus specify on {}...", project_root.display());

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

/// Filter specs data to only functions that have requires or ensures.
fn get_functions_with_specs(specs_data: &HashMap<String, Value>) -> HashMap<String, Value> {
    specs_data
        .iter()
        .filter(|(_, func_info)| {
            let has_requires = func_info
                .get("has_requires")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let has_ensures = func_info
                .get("has_ensures")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            has_requires || has_ensures
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Get functions with specs from blueprint.json based on type-status.
fn get_blueprint_functions_with_specs(blueprint_path: &Path) -> Result<HashMap<String, Value>> {
    if !blueprint_path.exists() {
        eprintln!("Warning: {} not found", blueprint_path.display());
        return Ok(HashMap::new());
    }

    let content = std::fs::read_to_string(blueprint_path)?;
    let blueprint_data: HashMap<String, Value> = serde_json::from_str(&content)?;

    let result: HashMap<String, Value> = blueprint_data
        .into_iter()
        .filter(|(_, node_info)| {
            let type_status = node_info
                .get("type-status")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            BLUEPRINT_SPEC_STATUSES.contains(&type_status)
        })
        .map(|(node_id, node_info)| (format!("veri:{}", node_id), node_info))
        .collect();

    Ok(result)
}
