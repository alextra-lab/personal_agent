#!/usr/bin/env python3
"""Verify documentation structure and hyperlinks.

This script:
1. Checks docs folder structure
2. Validates all markdown files
3. Checks internal links (relative paths)
4. Checks external links (HTTP/HTTPS)
5. Reports broken links and structural issues
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("Warning: requests library not found. External link checking will be disabled.")
    print("Install with: pip install requests")


class DocsLinkChecker:
    def __init__(self, docs_root: Path, check_external: bool = True, timeout: int = 5):
        self.docs_root = Path(docs_root).resolve()
        self.check_external = check_external and HAS_REQUESTS
        self.timeout = timeout
        self.all_md_files: Dict[Path, str] = {}
        self.link_map: Dict[Path, List[Tuple[str, str, str]]] = defaultdict(
            list
        )  # file -> [(link_text, link_url, link_type)]
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.stats = {
            "files_checked": 0,
            "internal_links": 0,
            "external_links": 0,
            "broken_internal": 0,
            "broken_external": 0,
        }

        if self.check_external:
            self.session = requests.Session()
            retry_strategy = Retry(
                total=2,
                backoff_factor=0.3,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
            self.session.headers.update(
                {"User-Agent": "Mozilla/5.0 (compatible; DocsLinkChecker/1.0)"}
            )

    def find_all_markdown_files(self) -> None:
        """Find all markdown files in docs directory."""
        for md_file in self.docs_root.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                rel_path = md_file.relative_to(self.docs_root)
                self.all_md_files[rel_path] = content
            except Exception as e:
                self.errors.append(f"Error reading {md_file}: {e}")

    def extract_links(self, file_path: Path, content: str) -> None:
        """Extract all links from markdown content."""
        # Pattern for markdown links: [text](url) or [text](url "title")
        link_pattern = r'\[([^\]]+)\]\(([^\)]+)(?:\s+"[^"]+")?\)'

        # Pattern for reference-style links: [text][ref] and [ref]: url
        ref_link_pattern = r"\[([^\]]+)\]\[([^\]]+)\]"
        ref_def_pattern = r"^\s*\[([^\]]+)\]:\s*(.+)$"

        # Find all inline links
        for match in re.finditer(link_pattern, content):
            link_text = match.group(1)
            link_url = match.group(2).strip()
            link_type = self._classify_link(link_url)

            # Skip citation links and code references
            if link_type == "skip":
                continue

            self.link_map[file_path].append((link_text, link_url, link_type))

            if link_type == "internal":
                self.stats["internal_links"] += 1
            elif link_type == "external":
                self.stats["external_links"] += 1

        # Find reference-style links
        ref_defs = {}
        for line in content.split("\n"):
            ref_match = re.match(ref_def_pattern, line)
            if ref_match:
                ref_id = ref_match.group(1).lower()
                ref_url = ref_match.group(2).strip()
                ref_defs[ref_id] = ref_url

        for match in re.finditer(ref_link_pattern, content):
            link_text = match.group(1)
            ref_id = match.group(2).lower()

            # Skip reference links that look like code (contain brackets, quotes, etc.)
            if any(char in link_text for char in ["[", "]", '"', "'", ".", ":", "-"]):
                continue

            if ref_id in ref_defs:
                link_url = ref_defs[ref_id]
                link_type = self._classify_link(link_url)

                # Skip citation links and code references
                if link_type == "skip":
                    continue

                self.link_map[file_path].append((link_text, link_url, link_type))

                if link_type == "internal":
                    self.stats["internal_links"] += 1
                elif link_type == "external":
                    self.stats["external_links"] += 1
            else:
                # Only warn if it doesn't look like code
                if not any(char in link_text for char in ["[", "]", '"', "'", ".", ":", "-"]):
                    self.warnings.append(
                        f"{file_path}: Reference link '[{link_text}][{ref_id}]' has no definition"
                    )

    def _classify_link(self, url: str) -> str:
        """Classify link as internal, external, anchor, or other."""
        url = url.strip()

        # Skip anchors only
        if url.startswith("#"):
            return "anchor"

        # Skip citation/protocol links (sediment://, etc.)
        if "://" in url and not url.startswith(("http://", "https://", "mailto:")):
            return "skip"  # Skip these

        # External links
        if url.startswith(("http://", "https://")):
            return "external"

        # Mailto links
        if url.startswith("mailto:"):
            return "mailto"

        # Internal links (relative paths)
        if url.startswith("./") or url.startswith("../") or "/" in url or url.endswith(".md"):
            return "internal"

        # Single-word links without extensions are likely code references, not files
        # Skip very short links that are probably code variables
        if len(url) <= 10 and not url.endswith(".md") and "/" not in url:
            return "skip"

        # Could be anchor or internal
        return "internal"

    def check_internal_link(self, file_path: Path, link_url: str) -> Tuple[bool, str]:
        """Check if an internal link is valid."""
        # Remove anchor if present
        if "#" in link_url:
            url_part, anchor = link_url.split("#", 1)
        else:
            url_part = link_url
            anchor = None

        # Get the directory of the current file (relative to docs_root)
        current_dir = file_path.parent

        # Handle relative paths
        if url_part.startswith("./"):
            # Relative to current file directory
            url_part = url_part[2:]
            target_path = current_dir / url_part
        elif url_part.startswith("../"):
            # Resolve relative to current file
            target_path = (self.docs_root / current_dir / url_part).resolve()
            if not target_path.is_relative_to(self.docs_root):
                # Allow links to parent directory (project root) for README.md
                if url_part == "../README.md" or url_part == "../../README.md":
                    return True, "OK (points to project root)"
                return False, "Link points outside docs directory"
            target_path = target_path.relative_to(self.docs_root)
        elif url_part.startswith("/"):
            # Absolute path from docs root
            target_path = Path(url_part[1:])  # Remove leading /
        else:
            # Relative to current file directory (no ./ prefix)
            target_path = current_dir / url_part

        # Resolve target file
        if str(target_path).endswith("/"):
            # Directory link - check for README.md
            target_file = self.docs_root / target_path / "README.md"
        else:
            # Try as exact file path (could be .md, .yaml, .yml, etc.)
            target_file = self.docs_root / target_path
            if not target_file.exists():
                # Try as .md file
                target_file = self.docs_root / f"{target_path}.md"
                if not target_file.exists():
                    # Try as directory with README
                    target_file = self.docs_root / target_path / "README.md"
                    if not target_file.exists():
                        # Try relative to project root (for config files, etc.)
                        project_root = self.docs_root.parent
                        target_file = project_root / target_path
                        if target_file.exists():
                            return True, "OK (points to project file)"

        # Check if file exists
        if not target_file.exists():
            return False, f"File not found: {target_file.relative_to(self.docs_root)}"

        # Check anchor if present
        if anchor:
            anchor_valid = self._check_anchor(target_file, anchor)
            if not anchor_valid:
                return (
                    False,
                    f"Anchor '#{anchor}' not found in {target_file.relative_to(self.docs_root)}",
                )

        return True, "OK"

    def _check_anchor(self, file_path: Path, anchor: str) -> bool:
        """Check if an anchor exists in the markdown file."""
        try:
            content = file_path.read_text(encoding="utf-8")
            # Normalize anchor (markdown converts headers to lowercase, spaces to hyphens)
            normalized_anchor = anchor.lower().replace(" ", "-").replace("_", "-")

            # Check for headers that would generate this anchor
            # Markdown headers: # Title -> #title
            header_pattern = r"^#{1,6}\s+(.+)$"
            for line in content.split("\n"):
                match = re.match(header_pattern, line)
                if match:
                    header_text = match.group(1).strip()
                    header_anchor = header_text.lower().replace(" ", "-").replace("_", "-")
                    # Remove special characters
                    header_anchor = re.sub(r"[^\w\-]", "", header_anchor)
                    normalized_anchor = re.sub(r"[^\w\-]", "", normalized_anchor)
                    if header_anchor == normalized_anchor:
                        return True

            # Also check for explicit anchor definitions: <a id="anchor"></a>
            if f'id="{anchor}"' in content or f"id='{anchor}'" in content:
                return True

            return False
        except Exception:
            return False

    def check_external_link(self, url: str) -> Tuple[bool, str]:
        """Check if an external link is accessible."""
        if not self.check_external:
            return True, "Skipped (requests not available)"

        try:
            response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if response.status_code == 200:
                return True, f"OK ({response.status_code})"
            elif response.status_code in [301, 302, 303, 307, 308]:
                return True, f"Redirect ({response.status_code})"
            else:
                return False, f"HTTP {response.status_code}"
        except requests.exceptions.Timeout:
            return False, "Timeout"
        except requests.exceptions.ConnectionError:
            return False, "Connection error"
        except requests.exceptions.RequestException as e:
            return False, f"Error: {str(e)[:50]}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)[:50]}"

    def verify_all(self) -> None:
        """Run all verification checks."""
        print(f"📚 Scanning docs directory: {self.docs_root}")
        print()

        # Step 1: Find all markdown files
        print("Step 1: Finding all markdown files...")
        self.find_all_markdown_files()
        self.stats["files_checked"] = len(self.all_md_files)
        print(f"   Found {self.stats['files_checked']} markdown files")
        print()

        # Step 2: Extract links from all files
        print("Step 2: Extracting links...")
        for file_path, content in self.all_md_files.items():
            self.extract_links(file_path, content)
        print(f"   Found {self.stats['internal_links']} internal links")
        print(f"   Found {self.stats['external_links']} external links")
        print()

        # Step 3: Check internal links
        print("Step 3: Checking internal links...")
        for file_path, links in self.link_map.items():
            for link_text, link_url, link_type in links:
                if link_type == "internal":
                    # Skip links that look like code references (single words, no path indicators)
                    if (
                        not any(indicator in link_url for indicator in ["./", "../", "/", ".md"])
                        and len(link_url) <= 15
                    ):
                        continue
                    is_valid, message = self.check_internal_link(file_path, link_url)
                    if not is_valid:
                        self.stats["broken_internal"] += 1
                        self.errors.append(
                            f"{file_path}: Broken internal link '{link_text}' -> {link_url}\n"
                            f"  Reason: {message}"
                        )
        print(f"   Broken internal links: {self.stats['broken_internal']}")
        print()

        # Step 4: Check external links
        if self.check_external:
            print("Step 4: Checking external links...")
            external_links_checked = 0
            for file_path, links in self.link_map.items():
                for link_text, link_url, link_type in links:
                    if link_type == "external":
                        external_links_checked += 1
                        print(f"   Checking: {link_url[:60]}...", end="\r")
                        is_valid, message = self.check_external_link(link_url)
                        if not is_valid:
                            self.stats["broken_external"] += 1
                            self.errors.append(
                                f"{file_path}: Broken external link '{link_text}' -> {link_url}\n"
                                f"  Reason: {message}"
                            )
            print(f"   Checked {external_links_checked} external links")
            print(f"   Broken external links: {self.stats['broken_external']}")
            print()
        else:
            print("Step 4: Skipping external link checks (requests not available)")
            print()

    def print_report(self) -> None:
        """Print verification report."""
        print("=" * 70)
        print("DOCUMENTATION VERIFICATION REPORT")
        print("=" * 70)
        print()

        print("📊 Statistics:")
        print(f"   Files checked: {self.stats['files_checked']}")
        print(f"   Internal links: {self.stats['internal_links']}")
        print(f"   External links: {self.stats['external_links']}")
        print(f"   Broken internal: {self.stats['broken_internal']}")
        print(f"   Broken external: {self.stats['broken_external']}")
        print()

        if self.warnings:
            print(f"⚠️  Warnings ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   {warning}")
            print()

        if self.errors:
            print(f"❌ Errors ({len(self.errors)}):")
            for error in self.errors:
                print(f"   {error}")
            print()
        else:
            print("✅ No errors found! All links are valid.")
            print()

        # Summary
        total_broken = self.stats["broken_internal"] + self.stats["broken_external"]
        if total_broken == 0:
            print("✅ All links verified successfully!")
            return 0
        else:
            print(f"❌ Found {total_broken} broken link(s)")
            return 1


def main():
    parser = argparse.ArgumentParser(description="Verify documentation structure and hyperlinks")
    parser.add_argument(
        "--docs-dir", type=str, default="docs", help="Path to docs directory (default: docs)"
    )
    parser.add_argument("--skip-external", action="store_true", help="Skip external link checking")
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Timeout for external link checks in seconds (default: 5)",
    )

    args = parser.parse_args()

    docs_path = Path(args.docs_dir)
    if not docs_path.exists():
        print(f"Error: Docs directory not found: {docs_path}")
        return 1

    checker = DocsLinkChecker(
        docs_path, check_external=not args.skip_external, timeout=args.timeout
    )

    checker.verify_all()
    return checker.print_report()


if __name__ == "__main__":
    sys.exit(main())
