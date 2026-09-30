"""POM Compare — Audit Maven dependencies against Project Lightwell catalog.

What this script does
---------------------

1. **Parse** each ``pom.xml`` encountered under a given target directory.
   Handles namespace-prefixed XML (e.g. ``{http://maven.apache.org/POM/4.0.0}``).

2. **Extract** ``<properties>`` from the base POM and every ``<profile>`` block.

3. **Resolve** ``${...}`` placeholders in version strings, producing a fully
   resolved version for (a) the default build and (b) each Maven profile
   individually.

4. **Classify** dependencies:

   * *Always active* — versions no profile overrides (unconditional).
   * *Conditional* — versions that change depending on which profile activates.

5. **Match** each dependency against the catalog using the ``(group, artifact)`` key:

   * ``[OK UP-TO-DATE]`` — exact rhlw revision match.
   * ``[NEEDS UPDATE]`` — a newer ``.rhlw-NNNNN`` (or ``.redhat-NNNNN``) revision
     exists at the same base version.  These are returned as actionable updates.
   * ``[BASE VERSION MISMATCH]`` — the POM's base version does not appear in the
     catalog.  Flagged but **not** returned as actionable (requires a deliberate
     upstream upgrade, not a Lightwell revision bump).

6. Print an **ACTIONABLE UPDATES** summary table at the bottom, grouped by scope
   (default properties / per-profile overrides), plus a global summary across all
   scanned POM files.

Version semantics
-----------------

* **Base version** — the upstream component version before any vendor suffix.
  Example: ``2.7.18`` extracted from ``2.7.18.rhlw-00004``.

* **Revision** — the integer suffix after ``.rhlw-`` or ``.redhat-``.

* A bump in the *base version* (e.g. ``2.6.x → 2.7.x``) is flagged as
  non-actionable.  A bump only in the *revision* (e.g.
  ``.rhlw-00002 → .rhlw-00004``) is actionable.

Usage
-----

.. code-block:: bash

   # Linux / macOS
   python3 pom-compare.py <target-dir> <path-to-catalog.json>

   # Windows (PowerShell)
   python pom-compare.py <target-dir> <path-to-catalog.json>

The first argument is a directory to recursively scan for ``pom.xml`` files.
The second argument is the path to ``catalog.json`` (defaults to the script's own
directory if omitted).
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET


def load_local_json(filepath):
    """Loads the Lightwell catalog JSON from the local filesystem."""
    try:
        with open(filepath, encoding='utf-8-sig') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: catalog file not found at {filepath}")
        return {"offerings": []}


def check_catalog_freshness(filepath):
    """Warn if the catalog file is older than 48 hours."""
    catalog_age_hours = 999
    try:
        mtime = os.path.getmtime(filepath)
        age_seconds = time.time() - mtime
        catalog_age_hours = age_seconds / 3600
    except OSError:
        return

    if catalog_age_hours > 48:
        print("=" * 60)
        print("CATALOG FRESHNESS WARNING")
        print("=" * 60)
        print(f"Your catalog.json is {catalog_age_hours:.1f} hours old (threshold: 48h).")
        print()
        print("The remediated-Java catalog updates frequently. To ensure the")
        print("audit results are current, download a fresh copy from your lightwell console")
        print("which should look like this:")
        print()
        print("  https://lightwell-console-lightwell.apps.virt.na-launch.com/api/catalog")
        print()
        print(f"ACTION: Once a new catalog file is downloaded replace the old one")
        print("=" * 60)


def resolve_properties(text, properties):
    """Resolves Maven ${property} references in a string."""
    def replacer(match):
        prop_name = match.group(1)
        return properties.get(prop_name, match.group(0))

    return re.sub(r'\$\{(.+?)\}', replacer, text)


def extract_base_properties(root, ns):
    """Returns dict of properties from the top-level <properties> block."""
    properties = {}
    props_elem = root.find(f"{ns}properties")
    if props_elem is not None:
        for child in props_elem:
            local = child.tag.removeprefix(ns)
            properties[local] = child.text or ""
    return properties


def extract_profiles(root, ns):
    """Returns list of (profile_id_or_undefined, {prop_name: value}) for each profile."""
    profiles = []
    profiles_elem = root.find(f"{ns}profiles")
    if profiles_elem is None:
        return profiles

    for profile in profiles_elem.findall(f"{ns}profile"):
        id_elem = profile.find(f"{ns}id")
        pid = (id_elem.text or "undefined-profile") if id_elem is not None else "undefined-profile"

        p_props = profile.find(f"{ns}properties")
        props = {}
        if p_props is not None:
            for child in p_props:
                local = child.tag.removeprefix(ns)
                props[local] = child.text or ""

        if props:
            profiles.append((pid, props))

    return profiles


def extract_dependencies(root, ns):
    """Returns raw list of (group, artifact, version_template) without resolving properties."""
    dependencies_elem = root.find(f"{ns}dependencies")
    if dependencies_elem is None:
        return []

    result = []
    project_group = (root.find(f"{ns}groupId").text or "") if root.find(f"{ns}groupId") is not None else ""

    for dep in dependencies_elem.findall(f"{ns}dependency"):
        group_elem = dep.find(f"{ns}groupId")
        group = (group_elem.text if group_elem is not None else "") or project_group

        name_elem = dep.find(f"{ns}artifactId")
        name = name_elem.text if name_elem is not None else ""

        ver_elem = dep.find(f"{ns}version")
        version = (ver_elem.text if ver_elem is not None else "") or ""

        result.append((group, name, version))

    return result


def resolve_deps(raw_deps, properties):
    """Resolve property references in dependency versions."""
    resolved = []
    for group, name, version_template in raw_deps:
        version = resolve_properties(version_template, properties)
        resolved.append((group, name, version))
    return resolved


def _base_version(version):
    """Strip .rhlw-XXXX or .redhat-XXXX suffix to get the upstream base version."""
    return re.sub(r'\.(?:rhlw|redhat)-[0-9]+$', '', version)


def _rhlw_revision(version):
    """Return the integer revision from a .rhlw-NNNNN or .redhat-NNNNN suffix, or None."""
    m = re.search(r'\.(?:rhlw|redhat)-(\d+)$', version)
    return int(m.group(1)) if m else None


def _is_rhlw_version(version):
    """Return True if the version string carries a .rhlw- or .redhat- revision suffix."""
    return bool(re.search(r'\.(?:rhlw|redhat)-[0-9]+$', version))


def match_against_catalog(components, catalog_dict):
    """Print match status for a list of (group, name, version) tuples.
    Returns a set of (group, artifact, current_version, target_version) for deps that need updating.

    Only returns entries where the POM's base version matches a catalog base version.
    Dependencies requiring a base version bump are flagged but NOT returned as actionable updates.
    """
    needs_update = set()
    for group, name, version in components:
        display_name = f"{group}:{name}:{version}" if group else f"{name}:{version}"
        base_ver = _base_version(version)

        key = (group, name)

        if key not in catalog_dict:
            print(f"Not present in Lightwell Catalog --- {display_name}")
            continue

        available_versions_dict = catalog_dict[key]

        # Gather all catalog versions that share the same base version.
        same_base = {v for v in available_versions_dict if _base_version(v) == base_ver}

        # Exact match check
        exact_offerings = available_versions_dict.get(version, set())

        if exact_offerings:
            # Check if a newer rhlw revision exists within the same base version.
            current_rev = _rhlw_revision(version)
            newer_versions = []
            for cv in same_base:
                cr = _rhlw_revision(cv)
                if cr is not None and current_rev is not None and cr > current_rev:
                    newer_versions.append(cv)

            if newer_versions:
                best_newer = sorted(newer_versions, key=lambda v: _rhlw_revision(v), reverse=True)[0]
                print(f"[NEEDS UPDATE] rhlw revision behind --- {display_name} -> {best_newer}")
                needs_update.add((group, name, version, best_newer))
            else:
                print(f"[OK UP-TO-DATE] exact match in offering(s): {', '.join(sorted(exact_offerings))} --- {display_name}")
        elif same_base:
            # No exact match, but a Lightwell version exists at this base version.
            best = sorted(same_base, key=lambda v: _rhlw_revision(v) or 0, reverse=True)[0]
            offerings = ", ".join(sorted(available_versions_dict[best]))

            if not _is_rhlw_version(version):
                # POM is using a community version; an rhlw variant exists.
                print(f"[NEEDS UPDATE] POM uses non-rhlw version --- {display_name} -> {best} (offering(s): {offerings})")
                needs_update.add((group, name, version, best))
            else:
                # Some other mismatch on same base edge case
                print(f"Lightwell version available in offering(s): {offerings} --- {display_name} -> {best}")
        else:
            # Component in catalog but no matching base version.
            latest_cat = sorted(available_versions_dict.keys())[-1]
            print(f"[BASE VERSION MISMATCH - NOT ACTIONABLE] POM uses base {base_ver} but catalog only has base {_base_version(latest_cat)}. Requires deliberate version bump, not a Lightwell update. --- {display_name}")

    return needs_update


# ---------------------------------------------------------------------------
# Helper: recursively find all pom.xml files under a directory
# ---------------------------------------------------------------------------
def find_pom_files(directory):
    """Walk the given directory tree and return sorted list of pom.xml paths."""
    pom_files = []
    for dirpath, _, filenames in os.walk(directory):
        if "pom.xml" in filenames:
            pom_files.append(os.path.join(dirpath, "pom.xml"))
    return sorted(pom_files)


# ---------------------------------------------------------------------------
# Helper: build a look-up dict from the downloaded catalog.json
# ---------------------------------------------------------------------------
def build_catalog_dict(catalog_path):
    """Load the catalog JSON and index by (group, artifact) -> {version -> set(offerings)}."""
    if not os.path.exists(catalog_path):
        print("Error: catalog.json not found.")
        print()
        print("Download it from your lightwell console which should look like this:")
        print("  https://lightwell-console-lightwell.apps.virt.na-launch.com/api/catalog")
        print()
        print(f"Then place it here: {catalog_path}")
        sys.exit(1)

    check_catalog_freshness(catalog_path)

    catalog_data = load_local_json(catalog_path)

    catalog_dict = {}

    for offering in catalog_data.get('offerings', []):
        offering_name = offering.get('name') or "Unknown Offering"

        for comp in offering.get('components', []):
            g = comp.get('g') or ""
            a = comp.get('a') or ""
            v = comp.get('v') or ""

            key = (g, a)
            if key not in catalog_dict:
                catalog_dict[key] = {}

            if v not in catalog_dict[key]:
                catalog_dict[key][v] = set()

            catalog_dict[key][v].add(offering_name)

    return catalog_dict


# ---------------------------------------------------------------------------
# Core audit: run a full dependency audit on a single POM file
# ---------------------------------------------------------------------------
def audit_single_pom(pom_path, catalog_dict):
    """Parse one pom.xml, classify dependencies, and compare against the catalog.
    Returns a list of (group, artifact, old_ver, new_ver) actionable updates."""

    print()
    print("-" * 60)
    print(f"AUDITING: {pom_path}")
    print("-" * 60)

    # -- Parse the XML tree --
    try:
        tree = ET.parse(pom_path)
    except FileNotFoundError:
        print(f"Error: POM file not found at {pom_path}")
        return []
    except ET.ParseError as e:
        print(f"Error parsing POM file: {e}")
        return []

    root = tree.getroot()

    # -- Detect namespace prefix --
    ns = ""
    m = re.match(r'\{(.+?)\}', root.tag)
    if m:
        ns = "{" + m.group(1) + "}"

    # -- Extract properties, profiles, and raw dependencies --
    base_properties = extract_base_properties(root, ns)
    profiles = extract_profiles(root, ns)
    raw_deps = extract_dependencies(root, ns)

    if not raw_deps:
        print("No dependencies found in the POM.")
        return []

    # Collect all actionable updates across every profile.
    all_needs_update = set()

    # -- Resolve dependency versions against base properties (default build) --
    base_components = resolve_deps(raw_deps, base_properties)

    # Identify which dependencies reference a property that a profile overrides.
    conditional_dep_indices = set()
    for pid, pprops in profiles:
        for dep_idx, (_, _, ver_tpl) in enumerate(raw_deps):
            used_props = re.findall(r'\$\{(.+?)\}', ver_tpl)
            for prop in used_props:
                if prop in pprops:
                    conditional_dep_indices.add(dep_idx)

    base_set = set(range(len(raw_deps)))
    always_active_indices = sorted(base_set - conditional_dep_indices)
    always_components = [base_components[i] for i in always_active_indices]

    # Collect per-section updates for labeled summaries.
    always_updates = set()
    default_updates = set()
    profile_updates = {}  # {profile_id: set of updates}

    # -- Audit always-active dependencies --
    print("=== ALWAYS ACTIVE (no profile overrides these) ===")
    if always_components:
        updated = match_against_catalog(always_components, catalog_dict)
        always_updates.update(updated or set())
        all_needs_update.update(always_updates)
    else:
        print("(none)")

    # -- Audit conditional dependencies (default/profile-independent resolution) --
    conditional_components = [base_components[i] for i in sorted(conditional_dep_indices)]

    if conditional_components:
        print()
        print("=== CONDITIONAL — DEFAULT PROPERTIES (property overrides not active) ===")
        updated = match_against_catalog(conditional_components, catalog_dict)
        default_updates.update(updated or set())
        all_needs_update.update(default_updates)

        # -- Audit conditional dependencies per-profile --
        for pid, pprops in profiles:
            merged = dict(base_properties)
            merged.update(pprops)
            profile_components = resolve_deps(raw_deps, merged)
            profile_conditional = [profile_components[i] for i in sorted(conditional_dep_indices)]

            print()
            print(f"=== CONDITIONAL — PROFILE ACTIVE: {pid} ===")
            updated = match_against_catalog(profile_conditional, catalog_dict)
            profile_updates[pid] = set(updated or set())
            all_needs_update.update(profile_updates[pid])

    # -- Per-POM actionable summary with section labels --
    print()
    print("=" * 60)
    print("ACTIONABLE UPDATES (base versions match catalog)")
    print("=" * 60)

    if always_updates:
        print()
        print("[DEFAULT PROPERTIES — always active]")
        for group, name, old_ver, new_ver in sorted(always_updates):
            display = f"{group}:{name}" if group else name
            print(f"  {display}  :  {old_ver}  ->  {new_ver}")

    if default_updates:
        print()
        print("[DEFAULT PROPERTIES — conditional, no profile active]")
        for group, name, old_ver, new_ver in sorted(default_updates):
            display = f"{group}:{name}" if group else name
            print(f"  {display}  :  {old_ver}  ->  {new_ver}")

    for pid, pset in profile_updates.items():
        if pset:
            print()
            print(f"[PROFILE '{pid}' overrides]")
            for group, name, old_ver, new_ver in sorted(pset):
                display = f"{group}:{name}" if group else name
                print(f"  {display}  :  {old_ver}  ->  {new_ver}")

    if not all_needs_update:
        print("  (none -- all dependencies are up-to-date)")

    return list(all_needs_update)


# ---------------------------------------------------------------------------
# Entry point: discover POMs, load catalog, audit each one
# ---------------------------------------------------------------------------
def main():
    _script_dir = os.path.dirname(os.path.abspath(__file__))

    # First argument: directory to scan for pom.xml files (defaults to current dir)
    target_path = sys.argv[1] if len(sys.argv) > 1 else "."

    # Second argument: path to the manually downloaded catalog.json
    catalog_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(_script_dir, "catalog.json")

    # -- Validate that the target is a directory --
    if not os.path.isdir(target_path):
        print(f"Error: '{target_path}' is not a directory.")
        sys.exit(1)

    # -- Load and index the catalog --
    catalog_dict = build_catalog_dict(catalog_path)

    # -- Recursively find all pom.xml files --
    pom_files = find_pom_files(target_path)

    if not pom_files:
        print(f"No pom.xml files found under '{target_path}'.")
        return

    print(f"Found {len(pom_files)} pom.xml file(s):")
    for p in pom_files:
        print(f"  - {p}")

    # -- Audit each POM independently --
    global_updates = []
    for pom_path in pom_files:
        updates = audit_single_pom(pom_path, catalog_dict)
        global_updates.extend(updates)

    # -- Global summary across all scanned POMs --
    print()
    print("=" * 60)
    print("GLOBAL SUMMARY — ALL ACTIONABLE UPDATES")
    print("=" * 60)
    if global_updates:
        unique = sorted(set(global_updates))
        for group, name, old_ver, new_ver in unique:
            display = f"{group}:{name}" if group else name
            print(f"  {display}  :  {old_ver}  ->  {new_ver}")
    else:
        print("  (none -- all dependencies are up-to-date)")


if __name__ == "__main__":
    main()
