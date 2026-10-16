"""Smoke tests for an installed mkdocs-material wheel.

Upstream ships no test suite, so these tests exercise the freshly built
distribution after it is installed into a clean virtual environment. Run them
from a working directory OUTSIDE the source checkout so that ``import
material`` resolves to the installed package, not the source tree.
"""

import functools
import http.server
import json
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

import pytest

try:  # Python 3.8+
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover
    import importlib_metadata  # type: ignore

EXPECTED_VERSION = "9.7.6"

EXPECTED_PLUGINS = [
    "blog",
    "group",
    "info",
    "meta",
    "offline",
    "optimize",
    "privacy",
    "projects",
    "search",
    "social",
    "tags",
    "typeset",
]


def _entry_points(group):
    """Return entry points for ``group`` (Python 3.8/3.9 and 3.10+ APIs)."""
    eps = importlib_metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=group))
    return list(eps.get(group, []))


def _material_plugin_eps():
    return {
        ep.name: ep
        for ep in _entry_points("mkdocs.plugins")
        if ep.name.startswith("material/")
    }


def _templates_dir():
    import material.templates

    return Path(material.templates.__file__).resolve().parent


def _source_checkout_root():
    """Directory of the source checkout this file lives in (.github/seal/..)."""
    return Path(__file__).resolve().parents[2]


def _run(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )


# ---------------------------------------------------------------------------
# 1. Package import and version
# ---------------------------------------------------------------------------


def test_material_imports_from_installed_location():
    import material

    pkg_dir = Path(material.__file__).resolve().parent
    # Must be the installed wheel, not the source checkout's material/ dir.
    assert _source_checkout_root() not in pkg_dir.parents


def test_distribution_version():
    assert importlib_metadata.version("mkdocs-material") == EXPECTED_VERSION


def test_module_version_consistent():
    import material

    version = getattr(material, "__version__", None)
    if version is None:
        pytest.skip("material.__version__ not defined")
    assert version == EXPECTED_VERSION


# ---------------------------------------------------------------------------
# 2. Plugin entry points
# ---------------------------------------------------------------------------


def test_plugin_entry_point_names():
    names = {name.split("/", 1)[1] for name in _material_plugin_eps()}
    assert names == set(EXPECTED_PLUGINS)


@pytest.mark.parametrize("name", EXPECTED_PLUGINS)
def test_plugin_entry_point_loads(name):
    from mkdocs.plugins import BasePlugin

    ep = _material_plugin_eps()["material/" + name]
    plugin_cls = ep.load()
    assert isinstance(plugin_cls, type)
    assert issubclass(plugin_cls, BasePlugin)


# ---------------------------------------------------------------------------
# 3. Theme entry point and packaged assets
# ---------------------------------------------------------------------------


def test_theme_entry_point_resolves_to_templates():
    eps = {ep.name: ep for ep in _entry_points("mkdocs.themes")}
    assert "material" in eps
    assert eps["material"].value == "material.templates"
    module = eps["material"].load()
    assert Path(module.__file__).resolve().parent == _templates_dir()


@pytest.mark.parametrize(
    "pattern",
    [
        "base.html",
        "main.html",
        "404.html",
        "mkdocs_theme.yml",
        "partials/search.html",
        "assets/javascripts/bundle.*.min.js",
        "assets/javascripts/workers/search.*.min.js",
        "assets/stylesheets/main.*.min.css",
        "assets/stylesheets/palette.*.min.css",
        ".icons/material/magnify.svg",
    ],
)
def test_theme_file_present(pattern):
    assert list(_templates_dir().glob(pattern)), pattern


def test_theme_config_declares_404_static_template():
    import yaml

    with open(str(_templates_dir() / "mkdocs_theme.yml"), encoding="utf-8") as f:
        theme_cfg = yaml.safe_load(f)
    assert isinstance(theme_cfg, dict)
    assert "404.html" in theme_cfg["static_templates"]


# ---------------------------------------------------------------------------
# 4. End-to-end site build with the material theme
# ---------------------------------------------------------------------------

MKDOCS_YML = textwrap.dedent(
    """\
    site_name: Seal Smoke Test
    theme:
      name: material
      features:
        - search.suggest
        - search.highlight
        - navigation.tabs
        - content.code.copy
      palette:
        scheme: slate
        primary: indigo
        accent: amber
    plugins:
      - search
    markdown_extensions:
      - admonition
      - pymdownx.highlight
      - pymdownx.superfences
      - pymdownx.tabbed:
          alternate_style: true
    nav:
      - Home: index.md
      - Page: page.md
    """
)

INDEX_MD = textwrap.dedent(
    """\
    # Welcome home

    !!! note "Sealed note"
        Admonition body text.

    ```python
    def hello():
        return "world"
    ```
    """
)

PAGE_MD = textwrap.dedent(
    """\
    # Second page

    === "Tab Alpha"

        Content of alpha.

    === "Tab Beta"

        Content of beta.

    Uniquezebraword appears here.
    """
)


@pytest.fixture(scope="module")
def built_site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site_build")
    (root / "docs").mkdir()
    (root / "mkdocs.yml").write_text(MKDOCS_YML, encoding="utf-8")
    (root / "docs" / "index.md").write_text(INDEX_MD, encoding="utf-8")
    (root / "docs" / "page.md").write_text(PAGE_MD, encoding="utf-8")
    result = _run(["mkdocs", "build", "--strict"], cwd=root)
    assert result.returncode == 0, result.stdout
    return root / "site"


def _index_html(site):
    return (site / "index.html").read_text(encoding="utf-8")


def _page_config(html):
    match = re.search(
        r'<script id="__config" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match, "__config script not found"
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    "rel_path",
    ["index.html", "page/index.html", "404.html", "search/search_index.json"],
)
def test_build_output_file(built_site, rel_path):
    assert (built_site / rel_path).is_file()


@pytest.mark.parametrize(
    "asset_glob",
    [
        "assets/javascripts/bundle.*.min.js",
        "assets/stylesheets/main.*.min.css",
    ],
)
def test_built_page_references_packaged_asset(built_site, asset_glob):
    packaged = sorted(p.name for p in _templates_dir().glob(asset_glob))
    assert len(packaged) == 1, packaged
    name = packaged[0]
    assert name in _index_html(built_site)
    assert (built_site / asset_glob.rsplit("/", 1)[0] / name).is_file()


def test_built_page_has_search_component(built_site):
    html = _index_html(built_site)
    assert 'data-md-component="search"' in html
    assert 'data-md-component="search-suggest"' in html


def test_built_page_config_features(built_site):
    config = _page_config(_index_html(built_site))
    for feature in (
        "search.suggest",
        "search.highlight",
        "navigation.tabs",
        "content.code.copy",
    ):
        assert feature in config["features"]
    assert "search" in config
    assert config["search"].endswith(".min.js")


def test_built_page_renders_markdown_extensions(built_site):
    html = _index_html(built_site)
    assert 'class="admonition note"' in html
    assert 'class="highlight"' in html
    page = (built_site / "page" / "index.html").read_text(encoding="utf-8")
    assert "tabbed-set" in page
    assert "Tab Alpha" in page and "Tab Beta" in page


def test_built_page_palette(built_site):
    html = _index_html(built_site)
    assert 'data-md-color-scheme="slate"' in html
    assert 'data-md-color-primary="indigo"' in html
    assert 'data-md-color-accent="amber"' in html


def test_search_index_contains_both_pages(built_site):
    with open(
        str(built_site / "search" / "search_index.json"), encoding="utf-8"
    ) as f:
        index = json.load(f)
    locations = {doc["location"] for doc in index["docs"]}
    assert "" in locations
    assert "page/" in locations
    assert any("Uniquezebraword" in doc["text"] for doc in index["docs"])


# ---------------------------------------------------------------------------
# 5. `mkdocs new` + build with the material theme
# ---------------------------------------------------------------------------


def test_mkdocs_new_then_build(tmp_path):
    project = tmp_path / "newproj"
    result = _run(["mkdocs", "new", str(project)], cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    cfg = project / "mkdocs.yml"
    assert cfg.is_file()
    with open(str(cfg), "a", encoding="utf-8") as f:
        f.write("\ntheme:\n  name: material\n")
    result = _run(["mkdocs", "build"], cwd=project)
    assert result.returncode == 0, result.stdout
    html = (project / "site" / "index.html").read_text(encoding="utf-8")
    assert 'data-md-component="search"' in html
    assert (project / "site" / "404.html").is_file()


# ---------------------------------------------------------------------------
# 6. Search suggestions render the query as text (CVE-2026-73295)
# ---------------------------------------------------------------------------

XSS_MARKER_PATH = "/__seal_xss"
XSS_REPORT_PATH = "/__seal_report"

# Deep-linked query (?q=...). The leading "-" makes the markup a prohibited
# search clause, so it is left out of the title query that produces the
# suggestion, while the trailing word completes to the page title below. The
# vulnerable suggest component wrote the whole query into the suggestion
# element via innerHTML, so the <img> tag was parsed and its onerror handler
# ran in the context of the documentation site.
XSS_PAYLOAD = "-<img/src/onerror=fetch('" + XSS_MARKER_PATH + "')> zebraquok"

XSS_MKDOCS_YML = textwrap.dedent(
    """\
    site_name: Seal Suggest Test
    theme:
      name: material
      features:
        - search.suggest
    plugins:
      - search
    extra_javascript:
      - seal_probe.js
    nav:
      - Zebraquokka guide: index.md
    """
)

XSS_INDEX_MD = textwrap.dedent(
    """\
    # Zebraquokka guide

    Documentation page used to produce a search suggestion.
    """
)

# Simulates the user interaction the suggest component waits for (focus on
# the search input), then reports what the suggestion element contains once
# the search worker produced a suggestion for the deep-linked query.
XSS_PROBE_JS = textwrap.dedent(
    """\
    (function () {
      var deadline = Date.now() + 45000
      function poll() {
        var query = document.querySelector("[data-md-component=search-query]")
        var suggest = document.querySelector("[data-md-component=search-suggest]")
        if (query)
          query.dispatchEvent(new Event("focus"))
        if (suggest && suggest.textContent.indexOf("zebraquok") !== -1) {
          setTimeout(function () {
            var params = new URLSearchParams()
            params.set("elements", String(suggest.querySelectorAll("*").length))
            params.set("text", suggest.textContent)
            params.set("html", suggest.innerHTML)
            fetch("%s?" + params.toString())
          }, 2000)
          return
        }
        if (Date.now() < deadline)
          setTimeout(poll, 250)
      }
      poll()
    })()
    """
    % XSS_REPORT_PATH
)


def _single_packaged_asset(pattern):
    assets = sorted(_templates_dir().glob(pattern))
    assert len(assets) == 1, assets
    return assets[0].read_text(encoding="utf-8")


def _find_browser():
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


class _RecordingHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the built site and record every requested path."""

    def do_GET(self):
        path, _, query = self.path.partition("?")
        self.server.seen.append((path, query))
        if path in (XSS_MARKER_PATH, XSS_REPORT_PATH):
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def test_search_suggest_bundle_does_not_render_query_as_html():
    bundle = _single_packaged_asset("assets/javascripts/bundle.*.min.js")
    # Vulnerable form: el.innerHTML = words.join("").replace(/\s/g, "&nbsp;")
    assert not re.search(
        r'\.innerHTML=\w+\.join\(""\)\.replace\(/\\s/g,"&nbsp;"\)', bundle
    )
    assert re.search(r'\.textContent=\w+\.join\(""\)', bundle)


def test_search_suggest_stylesheet_preserves_whitespace():
    css = _single_packaged_asset("assets/stylesheets/main.*.min.css")
    rules = re.findall(r"\.md-search__suggest\{([^}]*)\}", css)
    assert rules
    assert any(
        re.search(r"(?:^|;)white-space:pre(?:;|$)", rule) for rule in rules
    ), rules


def test_search_suggest_query_parameter_is_not_executed(tmp_path):
    browser = _find_browser()
    if browser is None:
        pytest.skip("no Chrome/Chromium browser available")

    root = tmp_path / "suggest_site"
    (root / "docs").mkdir(parents=True)
    (root / "mkdocs.yml").write_text(XSS_MKDOCS_YML, encoding="utf-8")
    (root / "docs" / "index.md").write_text(XSS_INDEX_MD, encoding="utf-8")
    (root / "docs" / "seal_probe.js").write_text(XSS_PROBE_JS, encoding="utf-8")
    result = _run(["mkdocs", "build", "--strict"], cwd=root)
    assert result.returncode == 0, result.stdout
    site = root / "site"

    handler = functools.partial(_RecordingHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = "http://127.0.0.1:%d/?q=%s" % (
        server.server_address[1],
        quote(XSS_PAYLOAD, safe=""),
    )
    chrome_log = tmp_path / "chrome.log"
    try:
        with open(str(chrome_log), "w") as log:
            proc = subprocess.Popen(
                [
                    browser,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--remote-debugging-port=0",
                    "--user-data-dir=%s" % (tmp_path / "profile"),
                    url,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.time() + 90
                while time.time() < deadline:
                    if any(p == XSS_REPORT_PATH for p, _ in server.seen):
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.25)
            finally:
                proc.kill()
                proc.wait()
    finally:
        server.shutdown()
        server.server_close()

    seen = list(server.seen)
    reports = [query for path, query in seen if path == XSS_REPORT_PATH]
    assert reports, "search suggestion never rendered: %r\n%s" % (
        seen,
        chrome_log.read_text(errors="replace")[-4000:],
    )
    report = parse_qs(reports[0], keep_blank_values=True)

    # The injected onerror handler must not have run ...
    assert XSS_MARKER_PATH not in [path for path, _ in seen], report
    # ... the query must not have been parsed into elements ...
    assert report["elements"] == ["0"], report
    # ... and the suggestion must show the query verbatim, as text.
    assert "<img/src/onerror=" in report["text"][0], report
    assert report["text"][0].endswith("zebraquokka"), report
