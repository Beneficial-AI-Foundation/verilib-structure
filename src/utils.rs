//! Utility functions for verilib structure.

use crate::config::constants::PROBE_VERUS_REPO;
use crate::{StructureForm, StructureType};
use anyhow::{bail, Context, Result};
use chrono::{DateTime, Utc};
use percent_encoding::{percent_decode_str, utf8_percent_encode, NON_ALPHANUMERIC};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::{self, BufRead, Write};
use std::path::Path;
use std::process::Command;

/// Certificate data stored in cert files
#[derive(Debug, Serialize, Deserialize)]
pub struct Cert {
    pub timestamp: DateTime<Utc>,
}

/// Encode an identifier for use as a filename.
///
/// Uses URL percent-encoding to replace special characters like '/', ':', '#', etc.
pub fn encode_name(name: &str) -> String {
    utf8_percent_encode(name, NON_ALPHANUMERIC).to_string()
}

/// Decode a filename back to an identifier.
pub fn decode_name(encoded: &str) -> String {
    percent_decode_str(encoded)
        .decode_utf8_lossy()
        .to_string()
}

/// Check if probe-verus is installed
pub fn check_probe_verus_installed() -> bool {
    which::which("probe-verus").is_ok()
}

/// Check if probe-verus is installed, exit with instructions if not.
pub fn check_probe_verus_or_exit() -> Result<()> {
    if !check_probe_verus_installed() {
        eprintln!("Error: probe-verus is not installed.");
        eprintln!("Please visit {} for installation instructions.", PROBE_VERUS_REPO);
        eprintln!();
        eprintln!("Quick install:");
        eprintln!("  git clone {}", PROBE_VERUS_REPO);
        eprintln!("  cd probe-verus");
        eprintln!("  cargo install --path .");
        bail!("probe-verus not installed");
    }
    Ok(())
}

/// Check if leanblueprint CLI tool is installed
pub fn check_leanblueprint_installed() -> bool {
    which::which("leanblueprint").is_ok()
}

/// Get the set of identifiers that already have certs.
pub fn get_existing_certs(certs_dir: &Path) -> Result<HashSet<String>> {
    let mut existing = HashSet::new();

    if !certs_dir.exists() {
        return Ok(existing);
    }

    for entry in std::fs::read_dir(certs_dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().map_or(false, |ext| ext == "json") {
            if let Some(stem) = path.file_stem() {
                let encoded_name = stem.to_string_lossy();
                let name = decode_name(&encoded_name);
                existing.insert(name);
            }
        }
    }

    Ok(existing)
}

/// Create a cert file for a function.
pub fn create_cert(certs_dir: &Path, name: &str) -> Result<std::path::PathBuf> {
    std::fs::create_dir_all(certs_dir)?;

    let encoded_name = encode_name(name);
    let cert_path = certs_dir.join(format!("{}.json", encoded_name));

    let cert = Cert {
        timestamp: Utc::now(),
    };

    let content = serde_json::to_string_pretty(&cert)?;
    std::fs::write(&cert_path, content)?;

    Ok(cert_path)
}

/// Delete a cert file for a function.
pub fn delete_cert(certs_dir: &Path, name: &str) -> Result<Option<std::path::PathBuf>> {
    let encoded_name = encode_name(name);
    let cert_path = certs_dir.join(format!("{}.json", encoded_name));

    if cert_path.exists() {
        std::fs::remove_file(&cert_path)?;
        Ok(Some(cert_path))
    } else {
        Ok(None)
    }
}

/// Get the set of identifier names from the structure.
pub fn get_structure_names(
    structure_type: StructureType,
    structure_form: StructureForm,
    structure_root: &Path,
    structure_json_path: &Path,
) -> Result<HashSet<String>> {
    let name_field = match structure_type {
        StructureType::Blueprint => "veri-name",
        StructureType::DalekLite => "code-name",
    };

    let mut names = HashSet::new();

    match structure_form {
        StructureForm::Json => {
            if !structure_json_path.exists() {
                eprintln!("Warning: {} not found", structure_json_path.display());
                return Ok(names);
            }

            let content = std::fs::read_to_string(structure_json_path)?;
            let structure: HashMap<String, Value> = serde_json::from_str(&content)?;

            for entry in structure.values() {
                if let Some(name) = entry.get(name_field).and_then(|v| v.as_str()) {
                    names.insert(name.to_string());
                }
            }
        }
        StructureForm::Files => {
            if !structure_root.exists() {
                eprintln!("Warning: {} not found", structure_root.display());
                return Ok(names);
            }

            for entry in walkdir::WalkDir::new(structure_root)
                .into_iter()
                .filter_map(|e| e.ok())
            {
                let path = entry.path();
                if path.extension().map_or(false, |ext| ext == "md") {
                    if let Ok(frontmatter) = parse_frontmatter(path) {
                        if let Some(name) = frontmatter.get(name_field).and_then(|v| v.as_str()) {
                            names.insert(name.to_string());
                        }
                    }
                }
            }
        }
    }

    Ok(names)
}

/// Parse YAML frontmatter from a markdown file.
pub fn parse_frontmatter(path: &Path) -> Result<HashMap<String, Value>> {
    let content = std::fs::read_to_string(path)?;
    let mut lines = content.lines();

    // Check for opening ---
    match lines.next() {
        Some("---") => {}
        _ => bail!("No frontmatter found"),
    }

    // Collect frontmatter lines until closing ---
    let mut yaml_lines = Vec::new();
    for line in lines {
        if line == "---" {
            break;
        }
        yaml_lines.push(line);
    }

    let yaml_content = yaml_lines.join("\n");
    let frontmatter: HashMap<String, Value> = serde_yaml::from_str(&yaml_content)
        .context("Failed to parse YAML frontmatter")?;

    Ok(frontmatter)
}

/// Write a markdown file with YAML frontmatter.
pub fn write_frontmatter_file(
    path: &Path,
    metadata: &HashMap<String, Value>,
    body: Option<&str>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let mut content = String::new();
    content.push_str("---\n");

    for (key, value) in metadata {
        let formatted = format_yaml_value(value)?;
        content.push_str(&format!("{}: {}\n", key, formatted));
    }

    content.push_str("---\n");
    content.push('\n');

    if let Some(body_content) = body {
        content.push_str(body_content);
        content.push('\n');
    }

    std::fs::write(path, content)?;
    Ok(())
}

/// Format a JSON value as a YAML scalar.
fn format_yaml_value(value: &Value) -> Result<String> {
    match value {
        Value::Null => Ok("null".to_string()),
        Value::Bool(b) => Ok(if *b { "true" } else { "false" }.to_string()),
        Value::Number(n) => Ok(n.to_string()),
        Value::String(s) => {
            // Check if string needs quoting
            if s.is_empty()
                || s == "null"
                || s == "true"
                || s == "false"
                || s == "~"
                || s.starts_with('{')
                || s.starts_with('[')
                || s.starts_with('\'')
                || s.starts_with('"')
                || s.starts_with('|')
                || s.starts_with('>')
                || s.starts_with('*')
                || s.starts_with('&')
                || s.starts_with('!')
                || s.contains(':')
                || s.contains('#')
                || s.contains('\n')
            {
                let escaped = s.replace('\\', "\\\\").replace('"', "\\\"").replace('\n', "\\n");
                Ok(format!("\"{}\"", escaped))
            } else {
                Ok(s.clone())
            }
        }
        Value::Array(arr) => {
            let items: Result<Vec<String>> = arr.iter().map(format_yaml_value).collect();
            Ok(format!("[{}]", items?.join(", ")))
        }
        Value::Object(_) => bail!("Nested objects are not supported in metadata"),
    }
}

/// Run an external command and return its output.
pub fn run_command(
    program: &str,
    args: &[&str],
    cwd: Option<&Path>,
) -> Result<std::process::Output> {
    let mut cmd = Command::new(program);
    cmd.args(args);

    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }

    let output = cmd.output().context(format!("Failed to run {}", program))?;
    Ok(output)
}

/// Display a multiple choice menu and get user selections.
pub fn display_menu<F>(
    items: &[(String, Value)],
    _structure_type: StructureType,
    format_item: F,
) -> Result<Vec<usize>>
where
    F: Fn(usize, &str, &Value) -> String,
{
    println!();
    println!("{}", "=".repeat(60));
    println!("Functions with specs but no certification:");
    println!("{}", "=".repeat(60));
    println!();

    for (i, (name, info)) in items.iter().enumerate() {
        println!("{}", format_item(i + 1, name, info));
        println!();
    }

    println!("{}", "=".repeat(60));
    println!();
    println!("Enter selection:");
    println!("  - Individual numbers: 1, 3, 5");
    println!("  - Ranges: 1-5");
    println!("  - 'all' to select all");
    println!("  - 'none' or empty to skip");
    println!();

    print!("Your selection: ");
    io::stdout().flush()?;

    let mut input = String::new();
    io::stdin().lock().read_line(&mut input)?;
    let input = input.trim().to_lowercase();

    if input.is_empty() || input == "none" {
        return Ok(vec![]);
    }

    if input == "all" {
        return Ok((0..items.len()).collect());
    }

    let mut selected = HashSet::new();
    for part in input.replace(',', " ").split_whitespace() {
        if part.contains('-') {
            let parts: Vec<&str> = part.splitn(2, '-').collect();
            if parts.len() == 2 {
                if let (Ok(start), Ok(end)) = (parts[0].parse::<usize>(), parts[1].parse::<usize>())
                {
                    for i in start..=end {
                        if i >= 1 && i <= items.len() {
                            selected.insert(i - 1);
                        }
                    }
                } else {
                    eprintln!("Warning: Invalid range '{}', skipping", part);
                }
            }
        } else if let Ok(idx) = part.parse::<usize>() {
            if idx >= 1 && idx <= items.len() {
                selected.insert(idx - 1);
            } else {
                eprintln!("Warning: {} out of range, skipping", idx);
            }
        } else {
            eprintln!("Warning: Invalid number '{}', skipping", part);
        }
    }

    let mut result: Vec<usize> = selected.into_iter().collect();
    result.sort();
    Ok(result)
}

/// Extract code path and line number from a GitHub link.
pub fn parse_github_link(github_link: &str) -> Option<(String, u32)> {
    if github_link.is_empty() || !github_link.contains("/blob/main/") {
        return None;
    }

    let path_part = github_link.split("/blob/main/").nth(1)?;

    if let Some((code_path, line_str)) = path_part.rsplit_once("#L") {
        let line_number: u32 = line_str.parse().ok()?;
        Some((code_path.to_string(), line_number))
    } else {
        Some((path_part.to_string(), 0))
    }
}

/// Get a display name from a full identifier.
pub fn get_display_name(name: &str) -> String {
    if name.starts_with("veri:") {
        name[5..].to_string()
    } else if let Some(pos) = name.rfind('#') {
        name[pos + 1..].trim_end_matches("()").to_string()
    } else {
        name.to_string()
    }
}
