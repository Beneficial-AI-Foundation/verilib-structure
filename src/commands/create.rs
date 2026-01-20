//! Create subcommand implementation.
//!
//! Initialize structure files from source analysis.

use crate::config::Config;
use crate::utils::{check_leanblueprint_installed, parse_github_link, run_command, write_frontmatter_file};
use crate::{StructureForm, StructureType};
use anyhow::{bail, Context, Result};
use regex::Regex;
use scraper::{Html, Selector};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

/// Run the create subcommand.
pub fn run(
    project_root: PathBuf,
    structure_type: StructureType,
    form: StructureForm,
    root: Option<PathBuf>,
) -> Result<()> {
    let project_root = project_root.canonicalize()
        .context("Failed to resolve project root")?;
    let verilib_path = project_root.join(".verilib");
    let structure_json_path = verilib_path.join("stubs.json");

    let (structure, structure_root_relative) = match structure_type {
        StructureType::Blueprint => {
            if root.is_some() {
                eprintln!("Warning: --root is ignored for blueprint type (fixed to 'blueprint')");
            }

            if !check_leanblueprint_installed() {
                eprintln!("Error: leanblueprint CLI is not installed or not in PATH");
                eprintln!("Install it with: pip install leanblueprint");
                bail!("leanblueprint not installed");
            }

            run_leanblueprint_web(&project_root)?;

            let blueprint_json_path = verilib_path.join("blueprint.json");
            let blueprint_data = generate_blueprint_json(&project_root, &blueprint_json_path)?;

            let structure = blueprint_to_structure(&blueprint_data);
            (structure, "blueprint".to_string())
        }

        StructureType::DalekLite => {
            let structure_root_relative = root
                .map(|r| r.to_string_lossy().to_string())
                .unwrap_or_else(|| ".verilib/structure".to_string());

            let tracked_path = project_root.join("functions_to_track.csv");
            if !tracked_path.exists() {
                bail!("{} not found", tracked_path.display());
            }

            let tracked_output_path = verilib_path.join("tracked_functions.csv");
            run_analyze_verus_specs_proofs(&project_root, &tracked_path, &tracked_output_path)?;

            let tracked = read_tracked_csv(&tracked_output_path)?;
            let tracked = tweak_disambiguate(tracked);
            let structure = tracked_to_structure(&tracked);

            (structure, structure_root_relative)
        }
    };

    // Write config file
    let config = Config::new(structure_type, form, &structure_root_relative);
    let config_path = config.save(&project_root)?;
    println!("Wrote config to {}", config_path.display());

    // Generate structure output
    println!("\nGenerating structure output...");
    match form {
        StructureForm::Json => {
            generate_structure_json(&structure, &structure_json_path)?;
        }
        StructureForm::Files => {
            let structure_root = project_root.join(&structure_root_relative);
            generate_structure_files(&structure, &structure_root)?;
        }
    }

    Ok(())
}

// =============================================================================
// Blueprint functions
// =============================================================================

/// Run 'leanblueprint web' to generate the blueprint/web folder.
fn run_leanblueprint_web(project_root: &Path) -> Result<()> {
    println!("Running 'leanblueprint web' to generate blueprint...");

    let output = run_command("leanblueprint", &["web"], Some(project_root))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error running leanblueprint web:\n{}", stderr);
        bail!("leanblueprint web failed");
    }

    println!("Successfully generated blueprint/web");
    Ok(())
}

/// Parse blueprint/web/dep_graph_document.html and generate blueprint.json.
fn generate_blueprint_json(
    project_root: &Path,
    output_path: &Path,
) -> Result<HashMap<String, Value>> {
    let html_path = project_root
        .join("blueprint")
        .join("web")
        .join("dep_graph_document.html");

    if !html_path.exists() {
        bail!("{} not found", html_path.display());
    }

    println!("Parsing {}...", html_path.display());
    let html_content = std::fs::read_to_string(&html_path)?;
    let document = Html::parse_document(&html_content);

    let node_info = get_node_info(&document)?;
    println!("Found {} dep-modal-container elements", node_info.len());

    let nodes = get_dep_graph(&document, &html_content, &node_info)?;
    println!("Parsed {} nodes from dependency graph", nodes.len());

    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let content = serde_json::to_string_pretty(&nodes)?;
    std::fs::write(output_path, content)?;
    println!("Wrote blueprint data to {}", output_path.display());

    Ok(nodes)
}

/// Extract all IDs and content of div elements with class "dep-modal-container".
fn get_node_info(document: &Html) -> Result<HashMap<String, String>> {
    let selector = Selector::parse("div.dep-modal-container").unwrap();
    let mut ids = HashMap::new();

    for div in document.select(&selector) {
        if let Some(div_id) = div.value().attr("id") {
            let children: Vec<_> = div.children().filter(|n| n.value().is_element()).collect();
            if children.len() != 1 {
                bail!("Div has {} children, expected 1", children.len());
            }

            let content = div.inner_html();
            if let Some(node_id) = div_id.strip_suffix("_modal") {
                ids.insert(node_id.to_string(), content);
            } else {
                bail!("Div ID does not end with _modal: {}", div_id);
            }
        }
    }

    Ok(ids)
}

/// Extract the renderDot content string from the HTML.
fn get_dep_graph_string(html_content: &str) -> Option<String> {
    let pattern = Regex::new(r"\.renderDot\(`([^`]*)`\)").ok()?;

    for caps in pattern.captures_iter(html_content) {
        let content = caps.get(1)?.as_str();
        if content.starts_with("strict digraph") {
            return Some(content.to_string());
        }
    }

    None
}

/// Parse attribute string like '[color=red, shape=box]' into a HashMap.
fn parse_attributes(attr_string: &str) -> HashMap<String, String> {
    let mut attrs = HashMap::new();
    let attr_string = attr_string.trim_matches(|c| c == '[' || c == ']');

    for attr in attr_string.split(',') {
        let attr = attr.trim();
        if let Some((key, value)) = attr.split_once('=') {
            let key = key.trim();
            let value = value.trim().trim_matches(|c| c == '"' || c == '\'');
            attrs.insert(key.to_string(), value.to_string());
        }
    }

    attrs
}

/// Parse the renderDot content and create a dictionary of nodes.
fn get_dep_graph(
    _document: &Html,
    html_content: &str,
    node_info: &HashMap<String, String>,
) -> Result<HashMap<String, Value>> {
    let renderdot_string = get_dep_graph_string(html_content)
        .context("Could not extract renderDot content from DOM")?;

    if !renderdot_string.starts_with("strict digraph \"\" {") {
        bail!("Content doesn't start with expected pattern");
    }

    let start_pos = "strict digraph \"\" {".len();
    let end_pos = renderdot_string.rfind('}').context("Could not find closing brace")?;

    let content = &renderdot_string[start_pos..end_pos];

    let mut nodes: HashMap<String, Value> = HashMap::new();
    let mut edges: Vec<(String, String, HashMap<String, String>)> = Vec::new();

    for element in content.split(';') {
        let element = element.trim();
        if element.is_empty() {
            continue;
        }

        // Skip graph/node/edge style definitions
        if element.starts_with("graph [")
            || element.starts_with("node [")
            || element.starts_with("edge [")
        {
            continue;
        }

        // Check if it's a node or edge
        if element.contains('[') && element.contains(']') && !element.contains("->") {
            // Parse node
            if let Some((node_info_parsed, _)) = parse_node_element(element) {
                nodes.insert(node_info_parsed.0, node_info_parsed.1);
            }
        } else if element.contains("->") {
            // Parse edge
            if let Some((source, target, attrs)) = parse_edge_element(element) {
                edges.push((source, target, attrs));
            }
        }
    }

    // Add content from node_info
    for (node_id, content) in node_info {
        if let Some(node) = nodes.get_mut(node_id) {
            if let Some(obj) = node.as_object_mut() {
                obj.insert("content".to_string(), json!(content));
            }
        } else {
            bail!("Node ID '{}' from node_info not found in parsed nodes", node_id);
        }
    }

    // Process edges to add dependencies
    for (source, target, attrs) in edges {
        if !nodes.contains_key(&source) || !nodes.contains_key(&target) {
            bail!("Source or target node not found: {} or {}", source, target);
        }

        let dep_type = if attrs.get("style").map(|s| s.as_str()) == Some("dashed") {
            "type-dependencies"
        } else {
            "term-dependencies"
        };

        if let Some(node) = nodes.get_mut(&source) {
            if let Some(obj) = node.as_object_mut() {
                if let Some(deps) = obj.get_mut(dep_type) {
                    if let Some(arr) = deps.as_array_mut() {
                        arr.push(json!(target));
                    }
                }
            }
        }
    }

    Ok(nodes)
}

/// Parse a node element line and return node information.
fn parse_node_element(line: &str) -> Option<((String, Value), String)> {
    let bracket_pos = line.find('[')?;
    let node_id = line[..bracket_pos].trim().trim_matches(|c| c == '"' || c == '\'');

    let attr_start = line.find('[')?;
    let attr_end = line.rfind(']')?;
    let attr_string = &line[attr_start..=attr_end];
    let attributes = parse_attributes(attr_string);

    let mut node_data = serde_json::Map::new();
    node_data.insert("kind".to_string(), json!(""));
    node_data.insert("content".to_string(), json!(""));
    node_data.insert("type-status".to_string(), json!(""));
    node_data.insert("term-status".to_string(), json!(""));
    node_data.insert("type-dependencies".to_string(), json!([]));
    node_data.insert("term-dependencies".to_string(), json!([]));

    // Process shape -> kind
    if let Some(shape) = attributes.get("shape") {
        let kind = match shape.as_str() {
            "ellipse" => "theorem",
            "box" => "definition",
            _ => return None, // Unknown shape
        };
        node_data.insert("kind".to_string(), json!(kind));
    }

    // Process color -> type-status
    if let Some(color) = attributes.get("color") {
        let type_status = match color.as_str() {
            "green" => "stated",
            "blue" => "can-state",
            "#FFAA33" => "not-ready",
            "darkgreen" => "mathlib",
            _ => "unrecognized",
        };
        node_data.insert("type-status".to_string(), json!(type_status));
    } else {
        node_data.insert("type-status".to_string(), json!("unknown"));
    }

    // Process fillcolor -> term-status
    if let Some(fillcolor) = attributes.get("fillcolor") {
        let term_status = match fillcolor.as_str() {
            "#9CEC8B" => "proved",
            "#B0ECA3" => "defined",
            "#A3D6FF" => "can-prove",
            "#1CAC78" => "fully-proved",
            _ => "unrecognized",
        };
        node_data.insert("term-status".to_string(), json!(term_status));
    } else {
        node_data.insert("term-status".to_string(), json!("unknown"));
    }

    Some(((node_id.to_string(), Value::Object(node_data)), line.to_string()))
}

/// Parse an edge element line and return edge information.
fn parse_edge_element(line: &str) -> Option<(String, String, HashMap<String, String>)> {
    let arrow_pos = line.find("->")?;
    let source = line[..arrow_pos].trim().trim_matches(|c| c == '"' || c == '\'');

    let remaining = line[arrow_pos + 2..].trim();
    let (target, attributes) = if let Some(attr_start) = remaining.find('[') {
        let target = remaining[..attr_start].trim().trim_matches(|c| c == '"' || c == '\'');
        let attr_end = remaining.rfind(']').unwrap_or(remaining.len());
        let attr_string = &remaining[attr_start..=attr_end];
        (target, parse_attributes(attr_string))
    } else {
        (remaining.trim_matches(|c| c == '"' || c == '\''), HashMap::new())
    };

    Some((source.to_string(), target.to_string(), attributes))
}

/// Convert blueprint data to a structure dictionary.
fn blueprint_to_structure(blueprint_data: &HashMap<String, Value>) -> HashMap<String, Value> {
    let mut result = HashMap::new();

    for (blueprint_id, attributes) in blueprint_data {
        let file_path = format!("{}.md", blueprint_id);

        let mut all_deps = Vec::new();
        if let Some(type_deps) = attributes.get("type-dependencies").and_then(|v| v.as_array()) {
            for dep in type_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }
        if let Some(term_deps) = attributes.get("term-dependencies").and_then(|v| v.as_array()) {
            for dep in term_deps {
                if let Some(s) = dep.as_str() {
                    all_deps.push(format!("veri:{}", s));
                }
            }
        }

        let content = attributes
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        result.insert(
            file_path,
            json!({
                "veri-name": format!("veri:{}", blueprint_id),
                "dependencies": all_deps,
                "content": content,
            }),
        );
    }

    result
}

// =============================================================================
// Dalek-lite functions
// =============================================================================

/// Run analyze_verus_specs_proofs.py CLI to generate tracked functions CSV.
fn run_analyze_verus_specs_proofs(
    project_root: &Path,
    seed_path: &Path,
    output_path: &Path,
) -> Result<()> {
    let script_path = project_root.join("scripts").join("analyze_verus_specs_proofs.py");
    if !script_path.exists() {
        bail!("Script not found: {}", script_path.display());
    }

    println!("Running analyze_verus_specs_proofs.py...");

    let seed_relative = seed_path
        .strip_prefix(project_root)
        .unwrap_or(seed_path);
    let output_relative = output_path
        .strip_prefix(project_root)
        .unwrap_or(output_path);

    // Ensure parent directory exists
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let output = run_command(
        "uv",
        &[
            "run",
            script_path.to_str().unwrap(),
            "--seed",
            seed_relative.to_str().unwrap(),
            "--output",
            output_relative.to_str().unwrap(),
        ],
        Some(project_root),
    )?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error running analyze_verus_specs_proofs.py:\n{}", stderr);
        bail!("analyze_verus_specs_proofs.py failed");
    }

    println!("Generated tracked functions CSV at {}", output_path.display());
    Ok(())
}

/// Tracked function data.
#[derive(Debug, Clone)]
#[allow(dead_code)]
struct TrackedFunction {
    has_spec: bool,
    has_proof: bool,
    is_external_body: bool,
    line_number: u32,
    link: String,
    function: String,
    module: String,
    qualified_name: String,
}

/// Read tracked functions CSV and return a HashMap.
fn read_tracked_csv(csv_path: &Path) -> Result<HashMap<String, TrackedFunction>> {
    let mut results = HashMap::new();
    let mut reader = csv::Reader::from_path(csv_path)?;

    for result in reader.records() {
        let record = result?;
        let function = record.get(0).unwrap_or("").to_string();
        let module = record.get(1).unwrap_or("").to_string();
        let link = record.get(2).unwrap_or("").to_string();
        let has_spec_val = record.get(3).unwrap_or("");
        let has_proof_val = record.get(4).unwrap_or("");

        let has_spec = has_spec_val == "yes" || has_spec_val == "ext";
        let is_external_body = has_spec_val == "ext";
        let has_proof = has_proof_val == "yes";

        let line_number = if link.contains("#L") {
            link.rsplit_once("#L")
                .and_then(|(_, l)| l.parse().ok())
                .unwrap_or(0)
        } else {
            0
        };

        let result_key = format!("{}::{}", function, module);
        results.insert(
            result_key,
            TrackedFunction {
                has_spec,
                has_proof,
                is_external_body,
                line_number,
                link,
                function: function.clone(),
                module,
                qualified_name: function,
            },
        );
    }

    Ok(results)
}

/// Disambiguate tracked items that have the same qualified_name.
fn tweak_disambiguate(tracked: HashMap<String, TrackedFunction>) -> HashMap<String, TrackedFunction> {
    let mut name_counts: HashMap<String, usize> = HashMap::new();
    for func in tracked.values() {
        *name_counts.entry(func.qualified_name.clone()).or_insert(0) += 1;
    }

    let duplicates: HashSet<_> = name_counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(name, _)| name)
        .collect();

    if duplicates.is_empty() {
        return tracked;
    }

    let mut name_indices: HashMap<String, usize> = duplicates.iter().map(|n| (n.clone(), 0)).collect();
    let mut new_tracked = HashMap::new();

    for (key, mut func) in tracked {
        if duplicates.contains(&func.qualified_name) {
            let idx = name_indices.get_mut(&func.qualified_name).unwrap();
            func.qualified_name = format!("{}_{}", func.qualified_name, idx);
            *idx += 1;
        }
        new_tracked.insert(key, func);
    }

    new_tracked
}

/// Convert tracked functions to a structure dictionary.
fn tracked_to_structure(tracked: &HashMap<String, TrackedFunction>) -> HashMap<String, Value> {
    let mut result = HashMap::new();

    for func in tracked.values() {
        if let Some((code_path, line_start)) = parse_github_link(&func.link) {
            if code_path.is_empty() {
                continue;
            }

            let func_name = func.qualified_name.replace("::", ".");
            let file_path = format!("{}/{}.md", code_path, func_name);

            result.insert(
                file_path,
                json!({
                    "code-line": line_start,
                    "code-path": code_path,
                    "code-name": null,
                }),
            );
        }
    }

    result
}

// =============================================================================
// Output generation
// =============================================================================

/// Generate structure .md files from a structure dictionary.
fn generate_structure_files(structure: &HashMap<String, Value>, structure_root: &Path) -> Result<()> {
    let mut created_count = 0;

    for (relative_path_str, metadata) in structure {
        let file_path = structure_root.join(relative_path_str);

        if file_path.exists() {
            eprintln!("WARNING: File already exists, overwriting: {}", file_path.display());
        }

        let mut metadata_map: HashMap<String, Value> = if let Some(obj) = metadata.as_object() {
            obj.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
        } else {
            HashMap::new()
        };

        let body_content = metadata_map.remove("content");
        let body = body_content.as_ref().and_then(|v| v.as_str());

        write_frontmatter_file(&file_path, &metadata_map, body)?;
        created_count += 1;
    }

    println!("Created {} structure files in {}", created_count, structure_root.display());
    Ok(())
}

/// Write structure dictionary to a JSON file.
fn generate_structure_json(structure: &HashMap<String, Value>, output_path: &Path) -> Result<()> {
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let content = serde_json::to_string_pretty(structure)?;
    std::fs::write(output_path, content)?;
    println!("Wrote structure to {}", output_path.display());

    Ok(())
}
